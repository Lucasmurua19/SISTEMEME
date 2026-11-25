from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.main import create_app
from app.api.routes.simulation import csv_safe
from app.infrastructure.db import make_engine, make_session_factory, uow_factory
from app.infrastructure.orm import Base
from app.settings import Settings
from tests.harness import Harness


def build(run_seconds: int | None = None, **settings_kw):
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    harness = Harness(uow_factory(sf))
    harness.run(run_seconds) if run_seconds else harness.run_all()
    settings = Settings(database_url="sqlite://", data_mode="fixtures", rate_limit_per_minute=1000, **settings_kw)
    return harness, TestClient(create_app(settings, sf))


@pytest.fixture(scope="module")
def finished():
    harness, client = build()
    yield harness, client


@pytest.fixture(scope="module")
def midrun():
    """Instante 170 s: las tres señales del grupo 1 siguen vigentes."""
    harness, client = build(run_seconds=170)
    yield harness, client


class TestHealth:
    def test_live(self, finished):
        _, client = finished
        assert client.get("/health/live").json() == {"status": "ok"}

    def test_ready_reports_worker_sources_and_database(self, finished):
        _, client = finished
        r = client.get("/health/ready")
        body = r.json()
        assert r.status_code == 200 and body["ready"] is True
        assert body["database"] == "ok" and body["worker"]["alive"] is True
        assert {s["stream"] for s in body["sources"]} >= {"migrations", "market", "quotes"}
        assert body["as_of"] and body["strategy"]["name"] == "fixtures-v1"

    def test_not_ready_without_worker(self):
        engine = make_engine("sqlite://")
        Base.metadata.create_all(engine)
        client = TestClient(create_app(Settings(database_url="sqlite://"), make_session_factory(engine)))
        r = client.get("/health/ready")
        assert r.status_code == 503 and r.json()["ready"] is False
        assert client.get("/api/v1/status").json()["overall"] == "down"


class TestCandidates:
    def test_eligible_ranking_is_ordered_by_score(self, midrun):
        _, client = midrun
        body = client.get("/api/v1/candidates?status=eligible").json()
        symbols = [i["symbol"] for i in body["items"]]
        assert symbols == ["ALFA", "BETA", "GAMA"]
        scores = [float(i["score"]) for i in body["items"]]
        assert scores == sorted(scores, reverse=True)
        assert body["no_entry"] is None
        assert body["as_of"] and body["strategy_version"]["name"] == "fixtures-v1"

    def test_each_row_has_the_documented_fields(self, midrun):
        _, client = midrun
        row = client.get("/api/v1/candidates?status=eligible").json()["items"][0]
        for key in ("mint", "pool", "score", "liquidity_lamports", "round_trip_cost_pct", "status", "reasons"):
            assert key in row
        assert isinstance(row["liquidity_lamports"], str)  # enteros grandes como cadenas

    def test_no_entry_with_reasons_when_nothing_is_eligible(self, finished):
        _, client = finished
        body = client.get("/api/v1/candidates?status=eligible").json()
        assert body["items"] == [] and body["total"] == 0
        assert body["as_of"]  # se muestra la última actualización aunque el ranking esté vacío
        assert body["no_entry"]["tracked"] >= 10
        codes = {r["code"] for r in body["no_entry"]["reasons"]}
        assert {"CONCENTRATION_TOO_HIGH", "DRAWDOWN_TOO_DEEP", "INSUFFICIENT_NET_BUYERS"} <= codes

    def test_status_filter_and_effective_status(self, finished):
        _, client = finished
        allc = {i["symbol"]: i for i in client.get("/api/v1/candidates?status=all&limit=200").json()["items"]}
        assert allc["NU"]["status"] == "pending_data"  # la fuente dejó de entregar ventanas
        assert "STALE_MARKET_DATA" in {r["code"] for r in allc["NU"]["reasons"]}
        assert allc["ALFA"]["status"] == "expired"
        assert "SIGNAL_EXPIRED" in {r["code"] for r in allc["ALFA"]["reasons"]}
        rejected = client.get("/api/v1/candidates?status=rejected&limit=200").json()
        assert {"ETA", "MU", "DELT", "LAMB", "XI"} <= {i["symbol"] for i in rejected["items"]}

    def test_pagination_and_validation(self, finished):
        _, client = finished
        page = client.get("/api/v1/candidates?status=all&limit=3&offset=2").json()
        assert len(page["items"]) == 3 and page["total"] > 3
        assert client.get("/api/v1/candidates?status=bogus").status_code == 422
        assert client.get("/api/v1/candidates?limit=9999").status_code == 422


class TestToken:
    def test_detail_exposes_pool_migration_evidence_and_position(self, finished):
        _, client = finished
        mint = next(i["mint"] for i in client.get("/api/v1/candidates?status=all&limit=200").json()["items"] if i["symbol"] == "ALFA")
        body = client.get(f"/api/v1/tokens/{mint}").json()
        pool = body["pools"][0]
        assert pool["migration"]["signature"] and pool["migration"]["slot"] > 0
        assert pool["verified"] is True and pool["canonical"] is True
        assert len(pool["snapshots"]) == 24
        ev = pool["evaluation"]
        assert {f["name"] for f in ev["filters"]} >= {"migration_verified", "concentration", "net_buyers", "round_trip_cost"}
        assert ev["pattern"]["peak"] and ev["pattern"]["low"] and ev["pattern"]["invalidation_level"]
        assert pool["signal"]["id"]
        closed = [p for p in pool["positions"] if p["status"] == "closed"]
        assert closed and closed[0]["attempts"][0]["kind"] == "entry"
        assert any(q["purpose"] == "evaluation" for q in pool["quotes"])

    def test_invalid_and_unknown_mints(self, finished):
        _, client = finished
        assert client.get("/api/v1/tokens/not-a-mint").status_code == 422
        assert client.get("/api/v1/tokens/" + "1" * 44).status_code == 404


class TestSimulation:
    def test_paper_trades_default_session_and_capital(self, finished):
        _, client = finished
        body = client.get("/api/v1/paper-trades").json()
        assert body["session"]["policy"] == "ranking"
        statuses = {p["symbol"]: p["status"] for p in body["positions"]}
        assert statuses["ALFA"] == "closed" and statuses["GAMA"] == "rejected" and statuses["KAPA"] == "open"
        kapa = next(p for p in body["positions"] if p["symbol"] == "KAPA")
        assert kapa["illiquid"] is True and kapa["zero_recovery_pnl_lamports"] == f"-{kapa['debited_lamports']}"
        assert body["session"]["capital"]["reserved_lamports"] == "0"

    def test_metrics_with_comparison_against_liquidity_reference(self, finished):
        _, client = finished
        body = client.get("/api/v1/metrics").json()
        m, cmp_ = body["metrics"], body["comparison"]
        assert m["trades_closed"] >= 3 and int(m["realized_pnl_lamports"]) > 0
        assert m["trades_illiquid"] == 1 and int(m["illiquid_cost_basis_lamports"]) > 0
        assert int(m["zero_recovery_scenario_pnl_lamports"]) < int(m["realized_pnl_lamports"])
        assert m["max_realized_drawdown_lamports"] is not None and m["failed_exit_rate"] is not None
        # abiertas valoradas junto al realizado: la ilíquida (KAPA) entra completa como pérdida
        assert int(m["open_pnl_valued_lamports"]) == int(m["open_valuation_lamports"]) - int(m["illiquid_cost_basis_lamports"])
        assert int(m["total_pnl_valued_lamports"]) == int(m["realized_pnl_lamports"]) + int(m["open_pnl_valued_lamports"])
        assert int(m["open_pnl_quoted_lamports"]) >= int(m["open_pnl_valued_lamports"])
        assert m["open_oldest_valuation_at"] is not None
        assert {"open_pnl_valued_lamports", "total_pnl_valued_lamports"} <= set(cmp_["delta"])
        # por posición: THET, OMIC y KAPA tuvieron salidas fallidas; ALFA y BETA no
        assert m["positions_with_failed_exit"] == 3 and m["positions_with_exit_trigger"] == 5
        assert cmp_["reference_session"]["policy"] == "liquidity"
        assert int(cmp_["delta"]["realized_pnl_lamports"]) > 0  # el ranking evitó la pérdida de GAMA en estos datos sintéticos
        assert {r["code"] for r in m["no_entry_reasons"]} >= {"POSITION_LIMIT", "SLIPPAGE_EXCEEDED"}
        assert body["fx"]["pair"] == "SOL/USD" and "evidencia" in body["disclaimer"]

    def test_chronological_block_filters_by_close_time(self, finished):
        _, client = finished
        full = client.get("/api/v1/metrics").json()["metrics"]["trades_closed"]
        first = client.get("/api/v1/metrics", params={"until": "2026-03-01T00:04:10+00:00"}).json()["metrics"]["trades_closed"]
        assert 0 < first < full
        assert client.get("/api/v1/metrics", params={"since": "2026-03-01T00:00:00"}).status_code == 422

    def test_ops_indicators(self, finished):
        _, client = finished
        body = client.get("/api/v1/ops").json()
        assert body["exit_attempts"]["failed"] > 0
        assert body["quotes"]["by_status"]["ok"] > 0 and body["jobs"]["by_status"]["done"] > 0
        assert body["stale_snapshot_pools"] >= 1
        assert any(r["code"] == "CONCENTRATION_TOO_HIGH" for r in body["candidates_discarded_by_reason"])

    def test_signals_for_debugging(self, finished):
        _, client = finished
        sigs = client.get("/api/v1/signals?limit=30").json()
        assert len(sigs) == 7 and sigs[0]["pattern"]["state"] == "confirmed"

    def test_csv_exports(self, finished):
        _, client = finished
        r = client.get("/api/v1/export/paper-trades.csv")
        assert r.headers["content-type"].startswith("text/csv")
        lines = r.text.strip().splitlines()
        assert lines[0].startswith("id,policy,symbol") and len(lines) > 5
        assert client.get("/api/v1/export/candidates.csv").status_code == 200

    def test_csv_formula_injection_is_neutralised(self):
        assert csv_safe("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
        assert csv_safe("+1") == "'+1" and csv_safe("@cmd") == "'@cmd" and csv_safe("ALFA") == "ALFA"
        assert csv_safe(5) == 5


class TestControlsAndSecurity:
    def test_pause_disabled_without_admin_token(self, finished):
        _, client = finished
        assert client.post("/api/v1/sessions/1/pause").status_code == 403

    def test_pause_requires_the_right_token_and_blocks_new_entries(self):
        harness, client = build(run_seconds=150, admin_token=SecretStr("s3cret"))
        assert client.post("/api/v1/sessions/1/pause", headers={"X-Admin-Token": "mal"}).status_code == 401
        r = client.post("/api/v1/sessions/1/pause", headers={"X-Admin-Token": "s3cret"}, json={"reason": "prueba"})
        assert r.json() == {"id": 1, "paused": True, "pause_reason": "prueba"}
        harness.run(300)  # la pausa persiste y las señales nuevas se registran como rechazadas
        body = client.get("/api/v1/paper-trades?session_id=1").json()
        assert body["session"]["paused"] is True
        assert {p["reject_reason"] for p in body["positions"]} == {"SESSION_PAUSED"}
        other = client.get("/api/v1/paper-trades?session_id=2").json()
        assert any(p["status"] != "rejected" for p in other["positions"])  # la otra sesión sigue operando
        assert client.post("/api/v1/sessions/1/resume", headers={"X-Admin-Token": "s3cret"}).json()["paused"] is False

    def test_security_headers_and_cors(self, finished):
        _, client = finished
        r = client.get("/health/live", headers={"Origin": "http://localhost:3000"})
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
        evil = client.get("/health/live", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in evil.headers

    def test_rate_limit_per_route(self):
        engine = make_engine("sqlite://")
        Base.metadata.create_all(engine)
        client = TestClient(create_app(Settings(database_url="sqlite://", rate_limit_per_minute=3), make_session_factory(engine)))
        codes = [client.get("/api/v1/status").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        assert client.get("/health/live").status_code == 200  # los health checks no se limitan
