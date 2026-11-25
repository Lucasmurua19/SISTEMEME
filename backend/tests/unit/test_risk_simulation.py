from datetime import timedelta
from decimal import Decimal

from app.domain.config import StrategyConfig
from app.domain.metrics import ClosedTrade, OpenTrade, compute_metrics, max_drawdown
from app.domain.models import PositionInfo, QuoteStatus, SessionState, SignalInfo
from app.domain.reasons import ReasonCode
from app.domain.risk import RiskLimits, check_entry, position_deficit
from app.domain.simulation import (
    check_exit_trigger,
    decide_entry,
    decide_exit_fill,
    exit_slippage_bps,
)
from tests.helpers import MINT, POOL, T0, build_series, make_quote

CFG = StrategyConfig().validate()
LIMITS = RiskLimits(100_000_000, 300_000_000, 3, 150_000_000)


def state(**kw):
    base = dict(
        capital_lamports=2_000_000_000,
        cash_lamports=2_000_000_000,
        reserved_lamports=0,
        active_positions=0,
        open_cost_basis_lamports=0,
        realized_pnl_lamports=0,
        open_deficit_lamports=0,
        paused=False,
    )
    base.update(kw)
    return SessionState(**base)


class TestRisk:
    def test_allows_within_budget(self):
        assert check_entry(LIMITS, state()).allowed

    def test_paused_blocks(self):
        d = check_entry(LIMITS, state(paused=True))
        assert (d.allowed, d.code) == (False, ReasonCode.SESSION_PAUSED)

    def test_position_limit(self):
        assert check_entry(LIMITS, state(active_positions=3)).code is ReasonCode.POSITION_LIMIT

    def test_exposure_counts_reserved_and_open(self):
        s = state(active_positions=2, reserved_lamports=100_000_000, open_cost_basis_lamports=100_000_000)
        assert check_entry(LIMITS, s).allowed  # 100+100+100 = 300 ≤ 300
        s = state(active_positions=2, reserved_lamports=150_000_000, open_cost_basis_lamports=100_000_000)
        assert check_entry(LIMITS, s).code is ReasonCode.EXPOSURE_LIMIT

    def test_session_loss_includes_open_deficit(self):
        s = state(realized_pnl_lamports=-60_000_000, open_deficit_lamports=90_000_000)
        assert check_entry(LIMITS, s).code is ReasonCode.SESSION_LIMIT
        s = state(realized_pnl_lamports=-60_000_000, open_deficit_lamports=40_000_000)
        assert check_entry(LIMITS, s).allowed

    def test_gains_offset_losses(self):
        # +30M realizados − 200M de déficit abierto = −170M neto → supera el límite de 150M
        s = state(realized_pnl_lamports=30_000_000, open_deficit_lamports=200_000_000)
        assert check_entry(LIMITS, s).code is ReasonCode.SESSION_LIMIT
        # +80M − 200M = −120M neto → todavía dentro del límite
        s = state(realized_pnl_lamports=80_000_000, open_deficit_lamports=200_000_000)
        assert check_entry(LIMITS, s).allowed

    def test_insufficient_capital(self):
        s = state(cash_lamports=150_000_000, reserved_lamports=100_000_000)
        assert check_entry(LIMITS, s).code is ReasonCode.INSUFFICIENT_CAPITAL

    def test_deficit_is_zero_recovery_when_no_route(self):
        assert position_deficit(100, None) == 100
        assert position_deficit(100, 120) == 0
        assert position_deficit(100, 70) == 30


def signal(**kw):
    base = dict(
        id=1,
        evaluation_id=1,
        pool=POOL,
        mint=MINT,
        strategy_version_id=1,
        status="active",
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=120),
        amount_lamports=100_000_000,
        invalidation_level=Decimal("0.78"),
        entry_ref_price=Decimal("0.95"),
        score=Decimal(70),
        liquidity_lamports=100,
        round_trip_cost_pct=Decimal(2),
        buy_quote_id=1,
        sell_quote_id=2,
        buy_price_lamports_per_unit=Decimal(2),
    )
    base.update(kw)
    return SignalInfo(**base)


def quotes(now, buy_out=50_000_000, sell_out=97_000_000):
    buy = make_quote("buy", in_amount=100_000_000, out_amount=buy_out, observed_at=now)
    sell = make_quote("sell", in_amount=buy_out, out_amount=sell_out, observed_at=now)
    return buy, sell


class TestEntry:
    now = T0 + timedelta(seconds=5)

    def test_accepts_when_nothing_changed(self):
        buy, sell = quotes(self.now)
        d = decide_entry(signal=signal(), buy=buy, sell=sell, latest=None, now=self.now, cfg=CFG)
        assert d.accepted and d.round_trip.cost_pct == Decimal(3)

    def test_price_jump_beyond_slippage_is_rejected_with_new_quote_recorded(self):
        buy, sell = quotes(self.now, buy_out=43_000_000)  # precio 2,33 vs 2,00 → +16 %
        d = decide_entry(signal=signal(), buy=buy, sell=sell, latest=None, now=self.now, cfg=CFG)
        assert (d.accepted, d.code) == (False, ReasonCode.SLIPPAGE_EXCEEDED)
        assert Decimal(d.detail["new_price"]) > Decimal(d.detail["limit_price"])

    def test_price_within_tolerance_is_accepted(self):
        buy, sell = quotes(self.now, buy_out=49_000_000)  # +2 % < 3 %
        assert decide_entry(signal=signal(), buy=buy, sell=sell, latest=None, now=self.now, cfg=CFG).accepted

    def test_missing_routes(self):
        buy, sell = quotes(self.now)
        dead_buy = make_quote("buy", in_amount=1, out_amount=0, status=QuoteStatus.NO_ROUTE, observed_at=self.now)
        dead_sell = make_quote("sell", in_amount=1, out_amount=0, status=QuoteStatus.NO_ROUTE, observed_at=self.now)
        assert decide_entry(signal=signal(), buy=dead_buy, sell=sell, latest=None, now=self.now, cfg=CFG).code is ReasonCode.ENTRY_QUOTE_UNAVAILABLE
        assert decide_entry(signal=signal(), buy=buy, sell=dead_sell, latest=None, now=self.now, cfg=CFG).code is ReasonCode.NO_SELL_ROUTE

    def test_expired_signal_stale_quote_and_invalidated_pattern(self):
        buy, sell = quotes(self.now)
        late = T0 + timedelta(seconds=500)
        assert decide_entry(signal=signal(), buy=buy, sell=sell, latest=None, now=late, cfg=CFG).code is ReasonCode.SIGNAL_EXPIRED
        old_buy, old_sell = quotes(self.now - timedelta(seconds=60))
        assert decide_entry(signal=signal(), buy=old_buy, sell=old_sell, latest=None, now=self.now, cfg=CFG).code is ReasonCode.QUOTE_STALE
        broken = build_series(["0.7"])[0]
        assert decide_entry(signal=signal(), buy=buy, sell=sell, latest=broken, now=self.now, cfg=CFG).code is ReasonCode.PATTERN_INVALIDATED

    def test_cost_over_limit_at_entry(self):
        buy, sell = quotes(self.now, sell_out=90_000_000)
        assert decide_entry(signal=signal(), buy=buy, sell=sell, latest=None, now=self.now, cfg=CFG).code is ReasonCode.ROUND_TRIP_COST_EXCEEDED


def position(**kw):
    base = dict(
        id=1,
        session_id=1,
        signal_id=1,
        pool=POOL,
        mint=MINT,
        status="open",
        tokens_expected=50_000_000,
        tokens_min=48_000_000,
        debited_lamports=100_000_000,
        reserved_lamports=0,
        entry_price=Decimal("0.95"),
        entry_liquidity_lamports=120_000_000_000,
        invalidation_level=Decimal("0.78"),
        opened_at=T0,
        exit_trigger=None,
        exit_failures=0,
        illiquid=False,
        next_exit_at=None,
        last_recoverable_lamports=None,
        last_valued_at=None,
    )
    base.update(kw)
    return PositionInfo(**base)


class TestExit:
    later = T0 + timedelta(seconds=60)

    def snap(self, close, **kw):
        s = build_series([close], p0=close, start=T0 + timedelta(seconds=30))[0]
        return s.__class__(**{**s.__dict__, **kw})

    def test_no_trigger_in_normal_conditions(self):
        assert check_exit_trigger(position=position(), latest=self.snap("0.95"), recoverable_lamports=100_000_000, now=self.later, cfg=CFG) is None

    def test_invalidation_has_priority(self):
        s = self.snap("0.7")
        t = check_exit_trigger(position=position(), latest=s, recoverable_lamports=200_000_000, now=self.later, cfg=CFG)
        assert t.code is ReasonCode.EXIT_INVALIDATION

    def test_liquidity_trigger(self):
        s = self.snap("0.95", sol_reserve_lamports=20_000_000_000)  # 40 / 120 = 33 %
        t = check_exit_trigger(position=position(), latest=s, recoverable_lamports=100_000_000, now=self.later, cfg=CFG)
        assert t.code is ReasonCode.EXIT_LIQUIDITY

    def test_take_profit_and_max_hold(self):
        t = check_exit_trigger(position=position(), latest=None, recoverable_lamports=121_000_000, now=self.later, cfg=CFG)
        assert t.code is ReasonCode.EXIT_TAKE_PROFIT
        t = check_exit_trigger(position=position(), latest=None, recoverable_lamports=100_000_000, now=T0 + timedelta(seconds=CFG.max_hold_s), cfg=CFG)
        assert t.code is ReasonCode.EXIT_MAX_HOLD

    def test_take_profit_disabled(self):
        cfg = StrategyConfig(take_profit_pct=None).validate()
        assert check_exit_trigger(position=position(), latest=None, recoverable_lamports=999_000_000, now=self.later, cfg=cfg) is None

    def test_failed_sale_keeps_reason_and_slippage_grows_per_retry(self):
        dead = make_quote("sell", in_amount=1, out_amount=0, status=QuoteStatus.NO_ROUTE, observed_at=self.later)
        d = decide_exit_fill(quote=dead, reference_out_lamports=100, attempt_number=1, now=self.later, cfg=CFG)
        assert (d.filled, d.cause) == (False, ReasonCode.NO_SELL_ROUTE)
        assert exit_slippage_bps(CFG, 1) == 500
        assert exit_slippage_bps(CFG, 2) == 800
        assert exit_slippage_bps(CFG, 99) == CFG.max_exit_slippage_bps

    def test_exit_slippage_vs_reference(self):
        q = make_quote("sell", in_amount=1, out_amount=90_000_000, observed_at=self.later)
        d = decide_exit_fill(quote=q, reference_out_lamports=100_000_000, attempt_number=1, now=self.later, cfg=CFG)
        assert d.cause is ReasonCode.SLIPPAGE_EXCEEDED  # -10 % con tolerancia 5 %
        d = decide_exit_fill(quote=q, reference_out_lamports=100_000_000, attempt_number=3, now=self.later, cfg=CFG)
        assert d.filled  # tolerancia 11 % en el 3.er intento
        assert d.proceeds_lamports == 90_000_000


class TestMetrics:
    def trade(self, i, debited, proceeds, adverse=None, minutes=0):
        return ClosedTrade(i, T0 + timedelta(minutes=minutes), debited, proceeds, adverse if adverse is not None else proceeds)

    def test_drawdown_on_realized_curve(self):
        assert max_drawdown([10, -5, -8, 20, -30, 5]) == 30
        assert max_drawdown([5, 5]) == 0
        assert max_drawdown([]) == 0

    def test_metrics(self):
        closed = [
            self.trade(1, 100, 130, minutes=1),  # +30
            self.trade(2, 100, 90, minutes=2),  # -10
            self.trade(3, 100, 70, minutes=3),  # -30
            self.trade(4, 100, 110, adverse=95, minutes=4),  # +10
        ]
        open_ = [OpenTrade(5, 100, 80, False), OpenTrade(6, 100, 0, True)]
        m = compute_metrics(closed, open_, capital_lamports=1000, exit_attempts_total=10, exit_attempts_failed=3, rejected_entries=2)
        assert m["realized_pnl_lamports"] == "0"
        assert Decimal(m["hit_rate"]) == Decimal("0.5")
        assert Decimal(m["expectancy_lamports"]) == 0
        assert m["max_realized_drawdown_lamports"] == "40"
        assert Decimal(m["max_realized_drawdown_pct_of_capital"]) == 4
        assert "max_drawdown_lamports" not in m  # el nombre viejo sugería que incluía lo abierto
        assert m["pnl_without_best_lamports"] == "-30"
        assert m["open_valuation_lamports"] == "-20"
        assert m["illiquid_cost_basis_lamports"] == "100"
        assert m["zero_recovery_scenario_pnl_lamports"] == "-100"
        assert Decimal(m["failed_exit_rate"]) == Decimal("0.3")
        assert m["adverse_pnl_lamports"] == "-15"  # +30 − 10 − 30 − 5 (el 4.º vende al mínimo garantizado)
        assert Decimal(m["profit_factor"]) == Decimal("1")

    def test_open_positions_valued_next_to_realized(self):
        closed = [self.trade(1, 100, 130, minutes=1)]  # +30 realizado
        open_ = [
            # con ruta: al mínimo garantizado recupera 80 (−20); al precio cotizado, 95 (−5)
            OpenTrade(2, 100, 80, False, quoted_recoverable_lamports=95, valued_at=T0 + timedelta(minutes=5)),
            # con ruta y ganancia latente: +10 al mínimo garantizado, +25 cotizado
            OpenTrade(3, 100, 110, False, quoted_recoverable_lamports=125, valued_at=T0 + timedelta(minutes=3)),
            # sin ruta de salida: recuperación cero (−100), sin importar la cotización previa
            OpenTrade(4, 100, 0, True, quoted_recoverable_lamports=0, valued_at=T0 + timedelta(minutes=4)),
        ]
        m = compute_metrics(closed, open_, capital_lamports=1000, exit_attempts_total=0, exit_attempts_failed=0)
        assert m["realized_pnl_lamports"] == "30"
        assert m["open_valuation_lamports"] == "-10"  # solo las que tienen ruta: −20 + 10
        assert m["open_pnl_valued_lamports"] == "-110"  # −20 + 10 − 100 (la ilíquida cuenta entera)
        assert m["open_pnl_quoted_lamports"] == "-80"  # −5 + 25 − 100: cota optimista, sigue negativa
        assert m["total_pnl_valued_lamports"] == "-80"  # 30 − 110
        assert m["open_oldest_valuation_at"] == (T0 + timedelta(minutes=3)).isoformat()
        # compatibilidad: el escenario viejo no suma lo abierto con ruta
        assert m["zero_recovery_scenario_pnl_lamports"] == "-70"

    def test_open_valuation_without_quoted_value_falls_back_to_conservative(self):
        m = compute_metrics([], [OpenTrade(1, 100, 80, False)], capital_lamports=1000, exit_attempts_total=0, exit_attempts_failed=0)
        assert m["open_pnl_valued_lamports"] == m["open_pnl_quoted_lamports"] == "-20"
        assert m["open_oldest_valuation_at"] is None

    def test_empty(self):
        m = compute_metrics([], [], capital_lamports=1000, exit_attempts_total=0, exit_attempts_failed=0)
        assert m["trades_closed"] == 0 and m["hit_rate"] is None and m["expectancy_lamports"] is None
        assert m["open_pnl_valued_lamports"] == m["total_pnl_valued_lamports"] == "0"
