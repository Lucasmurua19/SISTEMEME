"""Reintentos de salida: referencia propia de cada intento, valoración al día y espera creciente.

La primera prueba reproduce el caso visto en vivo el 2026-11-26: una toma de ganancia falló por un 11 % de
deslizamiento, el precio se desplomó y la posición quedó 3 horas reintentando contra la cotización del disparo.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.application.process_position import execute_exit, monitor_positions
from app.domain.models import Quote, QuoteStatus
from app.domain.simulation import exit_retry_delay_s
from app.infrastructure import orm
from tests.harness import Harness, fixtures_strategy


class ScriptedQuotes:
    """Cotización de venta que fija la prueba; ``value=None`` significa sin ruta."""

    def __init__(self) -> None:
        self.value: int | None = None
        self.calls = 0

    def quote_sell(self, pool, tokens_in, slippage_bps, now) -> Quote:
        self.calls += 1
        out = self.value or 0
        return Quote(
            side="sell", input_mint=pool.mint, output_mint="So11111111111111111111111111111111111111112",
            in_amount=tokens_in, out_amount=out, min_out_amount=out * (10_000 - slippage_bps) // 10_000,
            observed_at=now, expires_at=now + timedelta(seconds=20),
            status=QuoteStatus.OK if out else QuoteStatus.NO_ROUTE,
        )  # fmt: skip


class Driver:
    def __init__(self, harness: Harness, session_factory) -> None:
        self.h = harness
        self.sessions = session_factory
        self.quotes = ScriptedQuotes()
        for _ in range(80):
            harness.run(15)
            with session_factory() as s:
                pos = s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.status == "open")).first()
                if pos is not None and pos.exit_trigger is None:
                    self.position_id, self.session_id, self.debited = pos.id, pos.session_id, pos.debited_lamports
                    # la otra sesión abre su propia posición sobre el mismo pool: se aparta para medir solo esta
                    for other in s.scalars(select(orm.PaperPosition).where(orm.PaperPosition.id != pos.id)):
                        other.status = "closed"
                    s.commit()
                    return
        raise AssertionError("el escenario no abrió ninguna posición")

    def position(self) -> orm.PaperPosition:
        with self.sessions() as s:
            return s.get(orm.PaperPosition, self.position_id)

    def attempts(self) -> list[orm.PaperAttempt]:
        with self.sessions() as s:
            return list(s.scalars(
                select(orm.PaperAttempt)
                .where(orm.PaperAttempt.position_id == self.position_id, orm.PaperAttempt.kind == "exit")
                .order_by(orm.PaperAttempt.number)
            ))  # fmt: skip

    def monitor(self) -> None:
        w = self.h.worker
        monitor_positions(uow_factory=w.uow, quotes=self.quotes, clock=self.h.clock, cfg_loader=w.cfg_for)

    def execute_due(self) -> str | None:
        w = self.h.worker
        for a in self.attempts():
            if a.status == "pending" and a.scheduled_at.replace(tzinfo=self.h.clock.now().tzinfo) <= self.h.clock.now():
                return execute_exit(
                    uow_factory=w.uow, quotes=self.quotes, fx=None, clock=self.h.clock,
                    cfg_loader=w.cfg_for, attempt_id=a.id,
                )  # fmt: skip
        return None

    def deficit(self) -> int:
        with self.h.worker.uow() as uow:
            return uow.sessions.state(self.session_id).open_deficit_lamports


@pytest.fixture()
def driver(uow, session_factory):
    return Driver(Harness(uow, only={"alpha_valid"}), session_factory)


def test_a_collapse_after_a_failed_sale_closes_at_the_new_price(driver):
    trigger_value = driver.debited * 125 // 100  # +25 %: dispara la toma de ganancia
    driver.quotes.value = trigger_value
    driver.monitor()

    driver.h.clock.advance(5)
    driver.quotes.value = trigger_value * 887 // 1000  # cae 11 %: supera la tolerancia del primer intento
    assert driver.execute_due() == "failed:SLIPPAGE_EXCEEDED"
    pos = driver.position()
    assert pos.status == "open" and pos.exit_failures == 1
    assert pos.last_recoverable_lamports == driver.quotes.value + pos.immobilized_lamports  # ya no la del disparo

    crashed = trigger_value * 4 // 100
    driver.quotes.value = crashed
    driver.monitor()
    assert len(driver.attempts()) == 1  # todavía en espera: no se programa otro intento

    driver.h.clock.advance(15)
    driver.monitor()
    pos = driver.position()
    assert pos.last_recoverable_lamports == crashed + pos.immobilized_lamports
    assert driver.deficit() > driver.debited * 8 // 10  # el límite de pérdida de la sesión ve la caída

    driver.h.clock.advance(5)
    assert driver.execute_due() == "closed"
    first, second = driver.attempts()
    assert (first.reference_out_lamports, second.reference_out_lamports) == (trigger_value, crashed)
    assert first.trigger == second.trigger == "EXIT_TAKE_PROFIT"  # el disparo original se conserva
    pos = driver.position()
    assert pos.status == "closed" and pos.pnl_lamports < -driver.debited * 8 // 10


def test_retries_are_spaced_out_and_valuation_stays_current(driver):
    cfg = fixtures_strategy()
    driver.quotes.value = driver.debited * 125 // 100
    driver.monitor()
    driver.quotes.value = None  # desaparece la ruta de venta
    driver.quotes.calls = 0

    failed_at, oldest_valuation_s = [], 0.0
    for _ in range(600):
        driver.h.clock.advance(1)
        if driver.execute_due():
            failed_at.append(driver.h.clock.now())
        driver.monitor()
        if failed_at:
            seen = driver.position().last_valued_at.replace(tzinfo=driver.h.clock.now().tzinfo)
            oldest_valuation_s = max(oldest_valuation_s, (driver.h.clock.now() - seen).total_seconds())

    gaps = [(b - a).total_seconds() for a, b in zip(failed_at, failed_at[1:], strict=False)]
    delays = [exit_retry_delay_s(cfg, n) for n in range(1, len(gaps) + 1)]
    assert len(gaps) >= 5
    for gap, delay in zip(gaps, delays, strict=True):
        assert delay + cfg.exit_delay_s <= gap <= delay + cfg.exit_delay_s + 1
    assert delays == sorted(delays) and delays[-1] > delays[0]
    assert oldest_valuation_s <= cfg.exit_retry_interval_s  # revalorada aun durante las esperas largas
    assert driver.quotes.calls <= 600 // cfg.exit_retry_interval_s + 3 * len(failed_at)  # sin consultar en cada ciclo
    pos = driver.position()
    assert pos.status == "open" and pos.illiquid and pos.last_recoverable_lamports == 0

    driver.quotes.value = driver.debited // 2  # vuelve la ruta, a otro precio
    for _ in range(200):
        driver.h.clock.advance(1)
        driver.execute_due()
        driver.monitor()
        if driver.position().status == "closed":
            break
    assert driver.position().status == "closed"


def test_retry_delay_doubles_from_the_configured_interval_up_to_a_cap():
    cfg = fixtures_strategy(exit_retry_interval_s=15)
    assert [exit_retry_delay_s(cfg, n) for n in (1, 2, 3, 4, 5, 600)] == [15, 30, 60, 120, 120, 120]
    assert exit_retry_delay_s(fixtures_strategy(exit_retry_interval_s=300), 1) == 300
