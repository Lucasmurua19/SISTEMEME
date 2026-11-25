"""Aplicación FastAPI. Solo lectura sobre datos persistidos, salvo la pausa de la simulación."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app import __version__
from app.api.routes import health, radar, simulation
from app.api.security import RateLimitMiddleware, SecurityHeadersMiddleware
from app.infrastructure.db import make_engine, make_session_factory
from app.logging_config import configure_logging
from app.settings import Settings, get_settings

log = logging.getLogger("radar.api")


def create_app(settings: Settings | None = None, session_factory: sessionmaker[Session] | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if session_factory is None:
            configure_logging(settings.log_level)
            engine = make_engine(settings.database_url)
            app.state.session_factory = make_session_factory(engine)
            log.info("API lista (modo %s)", settings.data_mode)
            yield
            engine.dispose()
        else:
            app.state.session_factory = session_factory
            yield

    app = FastAPI(
        title="Radar de memecoins en Solana",
        version=__version__,
        description=(
            "Detecta migraciones de Pump.fun a PumpSwap, evalúa la primera caída con recuperación y mide "
            "el resultado de las señales con una cartera simulada. **Solo lectura on-chain; sin órdenes reales.**"
        ),
        lifespan=lifespan,
        docs_url="/docs",
    )
    app.state.settings = settings
    if session_factory is not None:
        app.state.session_factory = session_factory

    app.add_middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_minute)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["X-Admin-Token", "Content-Type"],
    )
    app.include_router(health.router)
    app.include_router(radar.router)
    app.include_router(simulation.router)
    return app


app = create_app()
