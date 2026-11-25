"""Configuración de estrategia versionada e inmutable.

Cada versión fija ventanas, umbrales, montos, costos máximos, caducidades y pesos del puntaje.
La huella (``fingerprint``) identifica el contenido exacto: dos versiones con el mismo nombre
pero distinta huella no pueden coexistir.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import MISSING, asdict, dataclass, fields
from decimal import Decimal
from typing import Any

_DECIMAL_FIELDS = {
    "min_drawdown_pct",
    "max_drawdown_pct",
    "min_recovery_pct",
    "invalidation_buffer_pct",
    "min_liquidity_retention_pct",
    "max_top10_concentration_pct",
    "max_round_trip_cost_pct",
    "min_coverage",
    "take_profit_pct",
    "liquidity_exit_retention_pct",
    "weight_recovery",
    "weight_liquidity",
    "weight_cost",
    "weight_holders",
    "score_recovery_full_pct",
    "score_concentration_best_pct",
}


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "radar-v1"
    description: str = "Primera caída con recuperación tras la migración a PumpSwap"

    # --- tamaño y riesgo de sesión
    trade_size_lamports: int = 100_000_000
    session_capital_lamports: int = 2_000_000_000
    max_exposure_lamports: int = 300_000_000
    max_positions: int = 3
    max_session_loss_lamports: int = 150_000_000

    # --- patrón
    window_seconds: int = 15
    max_age_after_migration_s: int = 900
    peak_window_s: int = 90
    min_drawdown_pct: Decimal = Decimal(20)
    max_drawdown_pct: Decimal = Decimal(70)
    min_recovery_pct: Decimal = Decimal(15)
    confirmation_windows: int = 1
    min_net_buyers: int = 5
    invalidation_buffer_pct: Decimal = Decimal(2)

    # --- filtros
    min_depth_lamports: int = 20_000_000_000
    min_depth_multiple: int = 200
    min_liquidity_retention_pct: Decimal = Decimal(60)
    max_top10_concentration_pct: Decimal = Decimal(50)
    concentration_max_age_s: int = 180
    max_round_trip_cost_pct: Decimal = Decimal(6)
    data_max_age_s: int = 45
    quote_max_age_s: int = 20
    min_coverage: Decimal = Decimal("0.8")

    # --- señal y ejecución simulada
    signal_ttl_s: int = 120
    entry_delay_s: int = 5
    exit_delay_s: int = 5
    slippage_bps: int = 300
    exit_slippage_bps: int = 500
    exit_slippage_step_bps: int = 300
    max_exit_slippage_bps: int = 3000
    ata_deposit_refundable: bool = True

    # --- salidas (se fijan antes de evaluar resultados)
    max_hold_s: int = 600
    take_profit_pct: Decimal | None = Decimal(20)
    liquidity_exit_retention_pct: Decimal = Decimal(45)
    exit_retry_max: int = 3
    exit_retry_interval_s: int = 15

    # --- ranking: pesos y rangos fijos por versión (el puntaje no depende de otros candidatos)
    weight_recovery: Decimal = Decimal(40)
    weight_liquidity: Decimal = Decimal(30)
    weight_cost: Decimal = Decimal(20)
    weight_holders: Decimal = Decimal(10)
    score_recovery_full_pct: Decimal = Decimal(60)
    score_concentration_best_pct: Decimal = Decimal(15)

    # ------------------------------------------------------------------ utilidades
    @property
    def min_depth_required_lamports(self) -> int:
        return max(self.min_depth_lamports, self.min_depth_multiple * self.trade_size_lamports)

    def validate(self) -> StrategyConfig:
        errors: list[str] = []
        weights = self.weight_recovery + self.weight_liquidity + self.weight_cost + self.weight_holders
        if weights != Decimal(100):
            errors.append(f"los pesos deben sumar 100 (suman {weights})")
        if not (Decimal(0) < self.min_drawdown_pct < self.max_drawdown_pct < Decimal(100)):
            errors.append("se requiere 0 < min_drawdown_pct < max_drawdown_pct < 100")
        if self.min_recovery_pct <= 0 or self.score_recovery_full_pct <= self.min_recovery_pct:
            errors.append("se requiere 0 < min_recovery_pct < score_recovery_full_pct")
        if self.max_top10_concentration_pct <= self.score_concentration_best_pct:
            errors.append("se requiere score_concentration_best_pct < max_top10_concentration_pct")
        if self.window_seconds <= 0 or self.peak_window_s < self.window_seconds:
            errors.append("se requiere window_seconds > 0 y peak_window_s >= window_seconds")
        if self.confirmation_windows < 1:
            errors.append("confirmation_windows debe ser >= 1")
        if self.trade_size_lamports <= 0 or self.max_positions < 1:
            errors.append("trade_size_lamports > 0 y max_positions >= 1")
        if self.max_exposure_lamports < self.trade_size_lamports:
            errors.append("max_exposure_lamports debe cubrir al menos una operación")
        if self.session_capital_lamports < self.trade_size_lamports:
            errors.append("session_capital_lamports debe cubrir al menos una operación")
        if self.max_round_trip_cost_pct <= 0:
            errors.append("max_round_trip_cost_pct debe ser > 0")
        if not (Decimal(0) < self.min_coverage <= Decimal(1)):
            errors.append("min_coverage debe estar en (0, 1]")
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            errors.append("take_profit_pct debe ser > 0 o nulo")
        if self.exit_retry_max < 1:
            errors.append("exit_retry_max debe ser >= 1")
        if errors:
            raise ValueError("StrategyConfig inválida: " + "; ".join(errors))
        return self

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            out[key] = str(value) if isinstance(value, Decimal) else value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyConfig:
        known = {f.name: f for f in fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            raise ValueError(f"campos desconocidos en la configuración: {sorted(unknown)}")
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key in _DECIMAL_FIELDS and value is not None:
                kwargs[key] = Decimal(str(value))
            else:
                kwargs[key] = value
        for f in known.values():  # claves faltantes usan el valor por defecto de la versión de código
            if f.name not in kwargs and f.default is MISSING:
                raise ValueError(f"falta el campo obligatorio {f.name}")
        return cls(**kwargs).validate()

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
