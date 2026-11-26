"""Snapshots, filtros, cotizaciones y ranking de los pools dentro de la ventana de estrategia."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.application.ports import (
    Clock,
    MarketDataSource,
    QuoteProvider,
    TransientSourceError,
    UnitOfWorkFactory,
)
from app.domain.config import StrategyConfig
from app.domain.models import PoolInfo, Quote, QuoteStatus
from app.domain.reasons import CandidateStatus
from app.domain.strategy import EvaluationInput, EvaluationResult, evaluate

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationOutcome:
    pool: str
    window_index: int | None
    status: CandidateStatus | None
    evaluation_id: int | None
    signal_id: int | None
    skipped: str | None = None
    error: str | None = None


def evaluate_pools(
    *,
    uow_factory: UnitOfWorkFactory,
    market: MarketDataSource,
    quotes: QuoteProvider,
    clock: Clock,
    version_id: int,
    cfg: StrategyConfig,
    budget_s: float | None = None,
    progress: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    shuffle: Callable[[list[PoolInfo]], None] = random.shuffle,
) -> list[EvaluationOutcome]:
    """Evalúa la última ventana cerrada de cada pool activo (una evaluación por pool, ventana y versión).

    Con ``budget_s`` el trabajo del ciclo tiene un tope de tiempo real: al agotarse no se empiezan más pools y el
    resto queda para el próximo ciclo. Con un RPC lento esto evita que la evaluación bloquee el latido, los trabajos
    y el seguimiento de posiciones abiertas. Como el orden fijo dejaría sin atender a los últimos pools, con
    presupuesto se mezcla el orden en cada ciclo. Sin presupuesto (fixtures) el recorrido es completo y determinista.
    ``progress`` se invoca después de cada pool (el worker lo usa para el latido).
    """
    now = clock.now()
    grace = cfg.window_seconds * 2
    with uow_factory() as uow:
        pools = uow.chain.list_active_pools(now, cfg.max_age_after_migration_s + grace)
    deadline = None if budget_s is None else monotonic() + budget_s
    if budget_s is not None:
        shuffle(pools)

    outcomes: list[EvaluationOutcome] = []
    for pool in pools:
        if deadline is not None and outcomes and monotonic() > deadline:
            log.info("presupuesto de evaluación agotado: %d de %d pools este ciclo", len(outcomes), len(pools))
            break
        try:
            outcomes.append(
                evaluate_pool(
                    uow_factory=uow_factory, market=market, quotes=quotes, clock=clock,
                    pool=pool, version_id=version_id, cfg=cfg,
                )  # fmt: skip
            )
        except TransientSourceError as exc:
            # límite de tasa o nodo lento: esperado con proveedores de cupo bajo; se reintenta en el próximo ciclo
            log.warning("pool %s: fuente transitoria, se reintenta (%s)", pool.address[:8], exc)
            outcomes.append(EvaluationOutcome(pool.address, None, None, None, None, error=f"TransientSourceError: {exc}"))
        except Exception as exc:
            # un pool con datos inconsistentes o una fuente caída no detiene la evaluación de los demás
            log.exception("falló la evaluación del pool %s", pool.address)
            outcomes.append(EvaluationOutcome(pool.address, None, None, None, None, error=f"{type(exc).__name__}: {exc}"))
        if progress is not None:
            progress()
    return outcomes


def evaluate_pool(
    *,
    uow_factory: UnitOfWorkFactory,
    market: MarketDataSource,
    quotes: QuoteProvider,
    clock: Clock,
    pool: PoolInfo,
    version_id: int,
    cfg: StrategyConfig,
) -> EvaluationOutcome:
    now = clock.now()

    fetched = market.get_snapshots(pool, now)  # red
    with uow_factory() as uow:
        stored = uow.snapshots.upsert_many(fetched) if fetched else uow.snapshots.list_for_pool(pool.address, now)

    base = EvaluationInput(
        pool=pool.address,
        mint=pool.mint,
        migrated_at=pool.migrated_at,
        migration_verified=pool.verified,
        identity_ok=pool.identity_ok,
        quote_mint=pool.quote_mint,
        snapshots=stored,
        as_of=now,
    )
    result = evaluate(base, cfg)
    if result.window_index is None:
        return EvaluationOutcome(pool.address, None, None, None, None, skipped="sin ventanas cerradas")

    with uow_factory() as uow:
        if uow.evaluations.exists(pool.address, result.window_index, version_id):
            return EvaluationOutcome(pool.address, result.window_index, result.status, None, None, skipped="ya evaluada")

    buy: Quote | None = None
    sell: Quote | None = None
    if result.needs_quote:
        try:
            buy = quotes.quote_buy(pool, cfg.trade_size_lamports, cfg.slippage_bps, now)  # red
        except Exception as exc:  # el contrato pide no lanzar; se protege el ciclo igualmente
            buy = unavailable_quote("buy", pool, now, f"{type(exc).__name__}: {exc}")
        if buy.ok:
            try:
                sell = quotes.quote_sell(pool, buy.min_out_amount, cfg.slippage_bps, now)  # red
            except Exception as exc:
                sell = unavailable_quote("sell", pool, now, f"{type(exc).__name__}: {exc}")
        result = evaluate(
            EvaluationInput(**{**base.__dict__, "buy_quote": buy, "sell_quote": sell, "as_of": clock.now()}),
            cfg,
        )

    evaluated_at = clock.now()
    with uow_factory() as uow:
        buy_id = uow.quotes.save(buy, pool=pool.address, purpose="evaluation") if buy else None
        sell_id = uow.quotes.save(sell, pool=pool.address, purpose="evaluation") if sell else None
        evaluation_id = uow.evaluations.save(
            pool=pool.address,
            version_id=version_id,
            result=result,
            buy_quote_id=buy_id,
            sell_quote_id=sell_id,
            evaluated_at=evaluated_at,
        )
        signal_id = None
        if evaluation_id is not None and result.eligible and buy is not None and buy.ok:
            signal_id = _emit_signal(uow, evaluation_id, pool, version_id, cfg, result, buy, buy_id, sell_id, evaluated_at)
    return EvaluationOutcome(pool.address, result.window_index, result.status, evaluation_id, signal_id)


def _emit_signal(uow, evaluation_id, pool, version_id, cfg, result: EvaluationResult, buy: Quote, buy_id, sell_id, now):
    latest = result.latest_snapshot
    assert latest is not None and result.score is not None and result.round_trip is not None
    assert result.pattern.invalidation_level is not None
    signal_id = uow.signals.create(
        evaluation_id=evaluation_id,
        pool=pool.address,
        version_id=version_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=cfg.signal_ttl_s),
        amount_lamports=cfg.trade_size_lamports,
        invalidation_level=result.pattern.invalidation_level,
        entry_ref_price=latest.close,
        score=result.score,
        liquidity_lamports=latest.liquidity_lamports,
        round_trip_cost_pct=result.round_trip.cost_pct,
        buy_quote_id=buy_id,
        sell_quote_id=sell_id,
        buy_price_lamports_per_unit=Decimal(buy.in_amount) / Decimal(buy.out_amount),
    )
    if signal_id is not None:
        uow.audit.log("signal", str(signal_id), "emitted", None, "active",
                      {"pool": pool.address, "score": str(result.score), "evaluation_id": evaluation_id}, now)  # fmt: skip
    return signal_id


def unavailable_quote(side: str, pool: PoolInfo, now, reason: str) -> Quote:
    """Cotización marcada como error, para fuentes que fallan sin lanzar."""
    from app.domain.models import WSOL_MINT

    return Quote(
        side=side,
        input_mint=WSOL_MINT if side == "buy" else pool.mint,
        output_mint=pool.mint if side == "buy" else WSOL_MINT,
        in_amount=0,
        out_amount=0,
        min_out_amount=0,
        observed_at=now,
        expires_at=now,
        status=QuoteStatus.ERROR,
        error=reason,
    )
