"""Ensamblado del modo en vivo: Solana RPC + PumpSwap + Jupiter (solo lectura)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.application.ports import Clock, UnitOfWorkFactory
from app.infrastructure.jupiter_quotes import JupiterClient, JupiterFxProvider, JupiterQuoteProvider
from app.infrastructure.solana_market import SolanaMarketSource
from app.infrastructure.solana_rpc import PumpMigrationSource, SolanaRpcClient
from app.infrastructure.solana_ws import MigrationLogListener
from app.settings import Settings

log = logging.getLogger(__name__)


def build_live_sources(settings: Settings, uow: UnitOfWorkFactory, clock: Clock):
    rpc_url = settings.solana_rpc_url.get_secret_value()
    if not rpc_url:
        raise RuntimeError("SOLANA_RPC_URL es obligatoria en DATA_MODE=live")
    rpc = SolanaRpcClient(rpc_url, commitment=settings.commitment)
    migrations = PumpMigrationSource(rpc, clock=clock, migration_account=settings.pump_migration_account)
    market = SolanaMarketSource(
        rpc,
        clock=clock,
        window_seconds=15,
        concentration_refresh_s=settings.concentration_refresh_s,
    )
    jupiter = JupiterClient(
        settings.jupiter_base_url, settings.jupiter_api_key.get_secret_value(), max_rps=settings.jupiter_max_rps
    )
    quotes = JupiterQuoteProvider(
        jupiter,
        priority_fee_lamports=settings.estimated_priority_fee_lamports,
        ata_deposit_lamports=settings.estimated_ata_deposit_lamports,
    )
    fx = JupiterFxProvider(jupiter)

    background: list[Callable[[threading.Event], None]] = []
    ws_url = settings.solana_ws_url.get_secret_value()
    if ws_url:

        def start_listener(stop: threading.Event) -> None:
            def enqueue(signature: str) -> None:
                now = clock.now()
                with uow() as u:
                    u.jobs.enqueue(
                        type="ingest_signature", payload={"signature": signature},
                        key=f"ingest_signature:{signature}", run_at=now, now=now,
                    )  # fmt: skip

            MigrationLogListener(ws_url, settings.pump_migration_account, settings.commitment, enqueue, stop).start()

        background.append(start_listener)
    else:
        log.warning("SOLANA_WS_URL vacía: solo se reconcilia por RPC")
    return migrations, market, quotes, fx, background
