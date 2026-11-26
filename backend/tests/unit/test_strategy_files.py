"""Archivos de estrategia por modo de datos: las reglas del modo live están congeladas en un archivo explícito."""

from __future__ import annotations

from dataclasses import replace

from app.bootstrap import DEFAULT_FIXTURE_STRATEGY, DEFAULT_LIVE_STRATEGY, load_strategy
from app.domain.config import StrategyConfig
from app.settings import Settings


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_each_mode_loads_its_own_frozen_file():
    fixtures = load_strategy(settings(data_mode="fixtures"))
    live = load_strategy(settings(data_mode="live"))
    assert (fixtures.name, live.name) == ("fixtures-v1", "live-v1")
    assert fixtures.fingerprint() != live.fingerprint()
    assert DEFAULT_FIXTURE_STRATEGY.endswith("strategy-fixtures-v1.json")
    assert DEFAULT_LIVE_STRATEGY.endswith("strategy-live-v1.json")


def test_live_rules_are_the_code_defaults_fixed_before_seeing_results():
    live = load_strategy(settings(data_mode="live"))
    defaults = StrategyConfig()
    # solo cambian nombre y descripción: ningún umbral se tocó para acomodarse a datos
    assert replace(live, name=defaults.name, description=defaults.description) == defaults


def test_rpc_url_guard_rejects_the_example_value_and_warns_about_the_public_endpoint(caplog):
    import pytest

    from app.infrastructure.live import check_rpc_url

    for bad in ("", "https://TU-PROVEEDOR-RPC/TU-CLAVE"):
        with pytest.raises(RuntimeError, match="valor de ejemplo"):
            check_rpc_url(bad)
    with caplog.at_level("WARNING", logger="app.infrastructure.live"):
        check_rpc_url("https://api.mainnet-beta.solana.com")
        check_rpc_url("https://mainnet.helius-rpc.com/?api-key=SECRET123")
    messages = [r.message for r in caplog.records]
    assert len(messages) == 1 and "RPC público" in messages[0]
    assert "SECRET123" not in "".join(messages)


def test_explicit_path_overrides_the_default_file():
    cfg = load_strategy(settings(data_mode="live", strategy_config_path=DEFAULT_FIXTURE_STRATEGY))
    assert cfg.name == "fixtures-v1"
