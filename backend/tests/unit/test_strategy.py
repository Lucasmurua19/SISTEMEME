from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.config import StrategyConfig
from app.domain.models import QuoteStatus
from app.domain.reasons import CandidateStatus, FilterOutcome, ReasonCode
from app.domain.strategy import (
    EvaluationInput,
    PatternState,
    RankItem,
    compute_score,
    detect_pattern,
    evaluate,
    liquidity_order,
    ranking_order,
)
from tests.helpers import MINT, POOL, T0, VALID_CLOSES, build_series, cheap_quotes, make_quote

CFG = StrategyConfig().validate()


def as_of_after(snaps, extra_s=2):
    return snaps[-1].window_end + timedelta(seconds=extra_s)


def inp(snaps, as_of=None, *, buy=None, sell=None, verified=True, identity=True, migrated_at=T0):
    return EvaluationInput(
        pool=POOL,
        mint=MINT,
        migrated_at=migrated_at,
        migration_verified=verified,
        identity_ok=identity,
        snapshots=snaps,
        as_of=as_of or as_of_after(snaps),
        buy_quote=buy,
        sell_quote=sell,
    )


def failing_codes(result):
    return {f.code for f in result.filters if f.outcome in (FilterOutcome.FAIL, FilterOutcome.PENDING)}


# ----------------------------------------------------------------------------- patrón
class TestPattern:
    def test_valid_rebound_is_confirmed_at_window_9(self):
        p = detect_pattern(build_series(VALID_CLOSES), T0, CFG)
        assert p.state is PatternState.CONFIRMED
        assert p.confirmed_window_index == 9
        assert p.peak.window_index == 2
        assert Decimal("38") < p.drawdown_pct < Decimal("40")
        assert p.invalidation_level < p.low.price  # buffer bajo el mínimo

    def test_not_confirmed_before_recovery(self):
        p = detect_pattern(build_series(VALID_CLOSES[:9]), T0, CFG)  # recuperación +13 %
        assert p.state is PatternState.DRAWDOWN

    def test_no_drop_keeps_tracking_peak(self):
        p = detect_pattern(build_series(["1.05", "1.1", "1.15", "1.2", "1.22", "1.25"]), T0, CFG)
        assert p.state is PatternState.TRACKING_PEAK

    def test_drawdown_beyond_max_is_too_deep_and_terminal(self):
        closes = ["1.1", "1.3", "1.2", "0.6", "0.35", "0.3", "0.45", "0.5", "0.6", "0.7"]
        p = detect_pattern(build_series(closes), T0, CFG)
        assert p.state is PatternState.TOO_DEEP  # un rebote posterior no lo revive

    def test_confirmed_pattern_is_frozen_and_later_data_cannot_rewrite_it(self):
        full = build_series(VALID_CLOSES + ["1.6", "2.0"])
        at_confirm = detect_pattern(full[:10], T0, CFG)
        later = detect_pattern(full, T0, CFG)
        assert later.state is PatternState.CONFIRMED
        assert later.peak == at_confirm.peak
        assert later.low == at_confirm.low
        assert later.invalidation_level == at_confirm.invalidation_level
        assert later.confirmed_window_index == at_confirm.confirmed_window_index

    def test_falling_under_invalidation_level_after_confirmation_expires(self):
        snaps = build_series(VALID_CLOSES + ["0.7"])
        p = detect_pattern(snaps, T0, CFG)
        assert p.state is PatternState.INVALIDATED
        assert p.invalidated_window_index == 10

    def test_peak_window_missing_is_no_data(self):
        snaps = build_series(["0.9"] * 3, start=T0 + timedelta(seconds=120))
        assert detect_pattern(snaps, T0, CFG).state is PatternState.NO_DATA

    def test_confirmation_requires_consecutive_windows(self):
        cfg = StrategyConfig(confirmation_windows=2).validate()
        # +19 % en una sola ventana y luego vuelve a caer a +10 %: no confirma
        closes = ["1.1", "1.2", "1.3", "1.1", "0.9", "0.85", "0.8", "0.95", "0.88", "0.9"]
        assert detect_pattern(build_series(closes), T0, cfg).state is PatternState.DRAWDOWN
        # dos ventanas consecutivas sobre +15 % confirman
        closes = ["1.1", "1.2", "1.3", "1.1", "0.9", "0.85", "0.8", "0.95", "0.96", "0.97"]
        assert detect_pattern(build_series(closes), T0, cfg).state is PatternState.CONFIRMED

    def test_confirmed_state_does_not_change_when_replayed_in_any_order(self):
        snaps = build_series(VALID_CLOSES)
        assert detect_pattern(list(reversed(snaps)), T0, CFG) == detect_pattern(snaps, T0, CFG)


# ----------------------------------------------------------------------------- evaluación
class TestEvaluate:
    def test_requests_quotes_only_when_prefilters_pass(self):
        snaps = build_series(VALID_CLOSES)
        res = evaluate(inp(snaps), CFG)
        assert res.needs_quote is True
        assert res.status is CandidateStatus.PENDING_DATA
        assert ReasonCode.QUOTE_PENDING in failing_codes(res)

    def test_valid_rebound_with_cheap_exit_is_eligible_with_score(self):
        snaps = build_series(VALID_CLOSES)
        now = as_of_after(snaps)
        buy, sell = cheap_quotes(now, cost_pct=2.5)
        res = evaluate(inp(snaps, now, buy=buy, sell=sell), CFG)
        assert res.status is CandidateStatus.ELIGIBLE, res.reasons
        assert res.reasons == ()
        assert 0 < res.score < 100
        assert res.round_trip.cost_pct == Decimal("2.5")
        assert {c.name for c in res.score_components} == {"recovery", "liquidity", "cost", "holders"}

    def test_rebound_without_volume_is_rejected(self):
        snaps = build_series(VALID_CLOSES, buyers=2)
        res = evaluate(inp(snaps), CFG)
        assert res.status is CandidateStatus.REJECTED
        assert ReasonCode.INSUFFICIENT_NET_BUYERS in failing_codes(res)
        assert res.needs_quote is False  # no se consulta al proveedor si ya falló un filtro

    def test_unknown_concentration_leaves_candidate_pending(self):
        snaps = build_series(VALID_CLOSES, concentration=None)
        res = evaluate(inp(snaps), CFG)
        assert res.status is CandidateStatus.PENDING_DATA
        assert ReasonCode.CONCENTRATION_UNKNOWN in failing_codes(res)

    def test_stale_concentration_counts_as_unknown(self):
        snaps = build_series(VALID_CLOSES)
        late = as_of_after(snaps) + timedelta(seconds=0)
        stale = [s.__class__(**{**s.__dict__, "concentration_observed_at": s.window_end - timedelta(seconds=999)}) for s in snaps]
        res = evaluate(inp(stale, late), CFG)
        assert ReasonCode.CONCENTRATION_UNKNOWN in failing_codes(res)

    def test_high_concentration_is_rejected(self):
        res = evaluate(inp(build_series(VALID_CLOSES, concentration="72")), CFG)
        assert res.status is CandidateStatus.REJECTED
        assert ReasonCode.CONCENTRATION_TOO_HIGH in failing_codes(res)

    def test_stale_market_data_is_pending(self):
        snaps = build_series(VALID_CLOSES)
        res = evaluate(inp(snaps, snaps[-1].window_end + timedelta(seconds=300)), CFG)
        assert res.status in (CandidateStatus.PENDING_DATA, CandidateStatus.EXPIRED)
        assert ReasonCode.STALE_MARKET_DATA in failing_codes(res)

    def test_no_data_at_all(self):
        res = evaluate(inp([], T0 + timedelta(seconds=30)), CFG)
        assert res.status is CandidateStatus.PENDING_DATA
        assert ReasonCode.NO_MARKET_DATA in failing_codes(res)

    def test_pattern_in_progress_is_observing(self):
        res = evaluate(inp(build_series(["1.05", "1.1", "1.2"])), CFG)
        assert res.status is CandidateStatus.OBSERVING
        assert ReasonCode.PATTERN_IN_PROGRESS in failing_codes(res)

    def test_drawdown_without_recovery_is_observing(self):
        res = evaluate(inp(build_series(VALID_CLOSES[:7])), CFG)
        assert res.status is CandidateStatus.OBSERVING
        assert ReasonCode.RECOVERY_NOT_CONFIRMED in failing_codes(res)

    def test_too_deep_is_rejected(self):
        closes = ["1.1", "1.3", "1.2", "0.6", "0.35", "0.3", "0.45", "0.5", "0.6", "0.7"]
        res = evaluate(inp(build_series(closes)), CFG)
        assert res.status is CandidateStatus.REJECTED
        assert ReasonCode.DRAWDOWN_TOO_DEEP in failing_codes(res)

    def test_invalidated_pattern_expires(self):
        res = evaluate(inp(build_series(VALID_CLOSES + ["0.7"])), CFG)
        assert res.status is CandidateStatus.EXPIRED
        assert ReasonCode.PATTERN_INVALIDATED in failing_codes(res)

    def test_age_beyond_limit_expires_even_with_pending_quote(self):
        snaps = build_series(VALID_CLOSES)
        old = T0 - timedelta(seconds=CFG.max_age_after_migration_s + 600)
        res = evaluate(inp(snaps, migrated_at=old), CFG)
        assert res.status is CandidateStatus.EXPIRED
        assert ReasonCode.PATTERN_EXPIRED in failing_codes(res)

    def test_unverified_migration_or_identity_is_rejected(self):
        snaps = build_series(VALID_CLOSES)
        assert evaluate(inp(snaps, verified=False), CFG).status is CandidateStatus.REJECTED
        assert evaluate(inp(snaps, identity=False), CFG).status is CandidateStatus.REJECTED

    def test_pool_quoted_in_another_token_is_rejected_with_its_own_reason(self):
        # caso real de mainnet: migración legítima de Pump.fun cuyo pool cotiza en otro token de Pump.fun
        snaps = build_series(VALID_CLOSES)
        base = inp(snaps)
        other = EvaluationInput(**{**base.__dict__, "quote_mint": "GexThZHgaswpAQ7Fi8QrHFubjPimUNSYhJiAA7XZpump"})
        res = evaluate(other, CFG)
        assert res.status is CandidateStatus.REJECTED
        codes = failing_codes(res)
        assert ReasonCode.UNSUPPORTED_QUOTE_MINT in codes
        assert ReasonCode.IDENTITY_MISMATCH not in codes and ReasonCode.MIGRATION_UNVERIFIED not in codes  # no es un fallo de verificación
        assert not res.needs_quote  # no se gasta ninguna cotización en algo fuera de alcance
        assert ReasonCode.UNSUPPORTED_QUOTE_MINT not in failing_codes(evaluate(base, CFG))  # con SOL el filtro pasa

    def test_insufficient_depth(self):
        res = evaluate(inp(build_series(VALID_CLOSES, sol0=5_000_000_000)), CFG)
        assert ReasonCode.INSUFFICIENT_DEPTH in failing_codes(res)
        assert res.status is CandidateStatus.REJECTED

    def test_liquidity_deterioration(self):
        # LP retirado: la reserva de SOL cae sin que el precio lo refleje
        snaps = build_series(VALID_CLOSES, liquidity_scale=False)
        drained = snaps[:-1] + [snaps[-1].__class__(**{**snaps[-1].__dict__, "sol_reserve_lamports": 25_000_000_000})]
        res = evaluate(inp(drained), CFG)
        assert ReasonCode.LIQUIDITY_DETERIORATING in failing_codes(res)

    def test_no_sell_route_is_pending_not_rejected(self):
        snaps = build_series(VALID_CLOSES)
        now = as_of_after(snaps)
        buy, _ = cheap_quotes(now)
        dead = make_quote("sell", in_amount=buy.min_out_amount, out_amount=0, status=QuoteStatus.NO_ROUTE, observed_at=now)
        res = evaluate(inp(snaps, now, buy=buy, sell=dead), CFG)
        assert res.status is CandidateStatus.PENDING_DATA
        assert ReasonCode.NO_SELL_ROUTE in failing_codes(res)
        assert res.score is None

    def test_cost_over_limit_is_rejected_but_scored_for_evidence(self):
        snaps = build_series(VALID_CLOSES)
        now = as_of_after(snaps)
        buy, sell = cheap_quotes(now, cost_pct=9)
        res = evaluate(inp(snaps, now, buy=buy, sell=sell), CFG)
        assert res.status is CandidateStatus.REJECTED
        assert ReasonCode.ROUND_TRIP_COST_EXCEEDED in failing_codes(res)
        assert res.score is not None

    def test_stale_quote_is_pending(self):
        snaps = build_series(VALID_CLOSES)
        now = as_of_after(snaps)
        buy, sell = cheap_quotes(now - timedelta(seconds=60))
        res = evaluate(inp(snaps, now, buy=buy, sell=sell), CFG)
        assert res.status is CandidateStatus.PENDING_DATA
        assert ReasonCode.QUOTE_STALE in failing_codes(res)

    def test_evaluation_ignores_observations_from_the_future(self):
        snaps = build_series(VALID_CLOSES)
        as_of = snaps[5].window_end + timedelta(seconds=2)  # solo se conocen las ventanas 0..5
        res = evaluate(inp(snaps, as_of), CFG)
        assert res.window_index == 5
        assert res.pattern.state is not PatternState.CONFIRMED

    def test_snapshot_not_yet_observed_is_invisible(self):
        snaps = build_series(VALID_CLOSES, lag_s=30)
        as_of = snaps[-1].window_end + timedelta(seconds=5)  # cada ventana se observa 30 s después de cerrar
        res = evaluate(inp(snaps, as_of), CFG)
        # as_of = 155 s: solo las ventanas que cerraron a ≤125 s ya habían sido observadas (índice 7)
        assert res.window_index == 7

    def test_evaluation_is_deterministic(self):
        snaps = build_series(VALID_CLOSES)
        now = as_of_after(snaps)
        buy, sell = cheap_quotes(now)
        a = evaluate(inp(snaps, now, buy=buy, sell=sell), CFG)
        b = evaluate(inp(list(reversed(snaps)), now, buy=buy, sell=sell), CFG)
        assert a.score == b.score and a.status == b.status and a.metrics == b.metrics


# ----------------------------------------------------------------------------- puntaje y orden
class TestScore:
    def test_weights_are_40_30_20_10_and_score_is_not_rounded(self):
        score, comps = compute_score(CFG, Decimal("37.5"), Decimal("80"), Decimal("3"), Decimal("32.5"))
        assert [(c.name, c.weight) for c in comps] == [
            ("recovery", 40),
            ("liquidity", 30),
            ("cost", 20),
            ("holders", 10),
        ]
        assert score == sum(c.score * c.weight / 100 for c in comps)
        assert score == Decimal("50")  # 50/50/50/50 con estos insumos

    def test_best_possible_inputs_score_100_and_worst_score_0(self):
        best, _ = compute_score(CFG, Decimal(60), Decimal(100), Decimal(0), Decimal(15))
        worst, _ = compute_score(CFG, Decimal(15), Decimal(60), Decimal(6), Decimal(50))
        assert best == 100
        assert worst == 0

    def test_score_does_not_depend_on_other_candidates(self):
        a = compute_score(CFG, Decimal(30), Decimal(75), Decimal(2), Decimal(25))[0]
        # cualquier otro cálculo previo no cambia el resultado: rangos fijos por versión
        compute_score(CFG, Decimal(60), Decimal(100), Decimal(0), Decimal(15))
        assert compute_score(CFG, Decimal(30), Decimal(75), Decimal(2), Decimal(25))[0] == a

    def test_ranking_orders_by_score_then_cost_then_fresh_quote(self):
        t = T0
        items = [
            RankItem(1, Decimal(70), Decimal(3), t, 100),
            RankItem(2, Decimal(80), Decimal(5), t, 50),
            RankItem(3, Decimal(70), Decimal(2), t, 10),
            RankItem(4, Decimal(70), Decimal(2), t + timedelta(seconds=5), 10),
        ]
        assert [i.key for i in ranking_order(items)] == [2, 4, 3, 1]

    def test_liquidity_reference_orders_by_liquidity(self):
        items = [
            RankItem(1, Decimal(90), Decimal(1), T0, 10),
            RankItem(2, Decimal(10), Decimal(1), T0, 500),
            RankItem(3, Decimal(50), Decimal(1), T0, 200),
        ]
        assert [i.key for i in liquidity_order(items)] == [2, 3, 1]


def test_config_validation_and_versioning():
    with pytest.raises(ValueError):
        StrategyConfig(weight_recovery=Decimal(50)).validate()  # pesos ≠ 100
    with pytest.raises(ValueError):
        StrategyConfig(min_drawdown_pct=Decimal(80)).validate()
    a = StrategyConfig()
    assert a.fingerprint() == StrategyConfig.from_dict(a.to_dict()).fingerprint()
    assert a.fingerprint() != StrategyConfig(min_net_buyers=9).fingerprint()
    with pytest.raises(ValueError):
        StrategyConfig.from_dict({**a.to_dict(), "campo_inventado": 1})
