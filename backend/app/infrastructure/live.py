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


PUBLIC_RPC_HOSTS = ("api.mainnet-beta.solana.com",)
EXAMPLE_PLACEHOLDER = "TU-PROVEEDOR"  # el valor de ejemplo de .env.example: nunca es una URL real


def check_rpc_url(rpc_url: str) -> None:
    """Falla con un mensaje claro ante una URL vacía o de ejemplo, y avisa si es el RPC público (sin exponer la URL)."""
    if not rpc_url or EXAMPLE_PLACEHOLDER in rpc_url:
        raise RuntimeError(
            "SOLANA_RPC_URL está vacía o conserva el valor de ejemplo de .env.example: configurá la URL de tu proveedor RPC"
        )
    if any(host in rpc_url for host in PUBLIC_RPC_HOSTS):
        log.warning(
            "se usa el RPC público de Solana: limita la tasa (HTTP 429, Retry-After) y no sostiene muchos pools a la vez. "
            "Sirve para una prueba de humo; para observar de forma continua usá un proveedor con cupo propio"
        )


def check_ws_url(ws_url: str) -> None:
    """Una URL de WebSocket sin completar solo produciría reconexiones sin fin: se avisa al arrancar."""
    if EXAMPLE_PLACEHOLDER in ws_url:
        raise RuntimeError(
            "SOLANA_WS_URL conserva el valor de ejemplo: configurá la URL wss de tu proveedor (o dejala vacía "
            "para reconciliar solo por RPC)"
        )


def build_live_sources(settings: Settings, uow: UnitOfWorkFactory, clock: Clock):
    rpc_url = settings.solana_rpc_url.get_secret_value()
    check_rpc_url(rpc_url)
    check_ws_url(settings.solana_ws_url.get_secret_value())
    rpc = SolanaRpcClient(rpc_url, commitment=settings.commitment, max_rps=settings.rpc_max_rps)
    migrations = PumpMigrationSource(
        rpc, clock=clock, migration_account=settings.pump_migration_account, budget_s=settings.ingest_budget_s
    )
    market = SolanaMarketSource(
        rpc,
        clock=clock,
        window_seconds=15,
        concentration_refresh_s=settings.concentration_refresh_s,
        poll_budget_s=settings.poll_budget_s,
        concurrency=settings.rpc_concurrency,
        bulk=settings.rpc_bulk_history,
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
