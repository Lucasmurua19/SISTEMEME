"""Configuración del proceso desde variables de entorno. Los secretos nunca se registran en logs."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- base de datos
    database_url: str = "postgresql+psycopg://radar:radar@localhost:5432/radar"

    # --- fuentes (solo servidor; nunca se envían al navegador)
    solana_rpc_url: SecretStr = SecretStr("https://api.mainnet-beta.solana.com")
    solana_ws_url: SecretStr = SecretStr("wss://api.mainnet-beta.solana.com")
    jupiter_api_key: SecretStr = SecretStr("")
    jupiter_base_url: str = "https://api.jup.ag/swap/v2"
    commitment: Literal["confirmed", "finalized"] = "confirmed"
    # Cuenta que participa en cada migración de Pump.fun (withdraw_authority): se consulta en lugar del
    # programa completo, que recibe todo el tráfico de la bonding curve. Verificar con la documentación vigente.
    pump_migration_account: str = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
    jupiter_max_rps: float = 0.0  # 0 = automático (0,5 sin clave, 1 con clave)
    estimated_priority_fee_lamports: int = 100_000
    estimated_ata_deposit_lamports: int = 2_039_280
    concentration_refresh_s: int = 60

    # --- modo de datos: "fixtures" reproduce escenarios grabados; "live" usa RPC y Jupiter
    data_mode: Literal["fixtures", "live"] = "fixtures"
    fixtures_dir: str = "fixtures/scenarios"
    fixture_step_seconds: int = 15

    # --- estrategia
    strategy_config_path: str | None = None

    # --- worker
    tick_seconds: float = 2.0
    worker_id: str = "worker-1"
    worker_max_silence_s: int = 30
    source_max_age_s: int = 120
    source_max_failures: int = 3
    job_claim_ttl_s: int = 60
    job_max_attempts: int = 6
    reconcile_page_limit: int = 100

    # --- API
    admin_token: SecretStr = SecretStr("")
    cors_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 300
    log_level: str = "INFO"

    # --- panel (Next.js lo lee del entorno del servidor)
    api_internal_url: str = Field(default="http://api:8000")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
