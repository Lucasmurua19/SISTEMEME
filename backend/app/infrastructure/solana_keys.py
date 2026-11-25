"""Claves públicas de Solana: base58, derivación de PDA y verificación de curva ed25519.

Una PDA es el primer ``sha256(seeds || bump || program_id || "ProgramDerivedAddress")`` (bump de 255 a 0)
que NO es un punto válido de la curva ed25519.
"""

from __future__ import annotations

import hashlib

import base58

from app.domain.models import PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID, WSOL_MINT

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
POOL_AUTHORITY_SEED = b"pool-authority"
POOL_SEED = b"pool"
CANONICAL_POOL_INDEX = 0


def b58decode32(address: str) -> bytes:
    raw = base58.b58decode(address)
    if len(raw) != 32:
        raise ValueError(f"clave pública de {len(raw)} bytes (se esperan 32)")
    return raw


def b58encode(raw: bytes) -> str:
    return base58.b58encode(raw).decode()


def is_on_curve(public_key: bytes) -> bool:
    """¿Los 32 bytes decodifican a un punto de la curva ed25519? (ecuación de la curva en Edwards)."""
    if len(public_key) != 32:
        return False
    y = int.from_bytes(public_key, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1  # criterio de Euler: x² debe ser residuo cuadrático


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    pid = b58decode32(program_id)
    for seed in seeds:
        if len(seed) > 32:
            raise ValueError("una semilla no puede superar 32 bytes")
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("no se encontró una PDA válida")


def pump_pool_authority(mint: str) -> str:
    """Autoridad del pool de migración: PDA de Pump con semillas ["pool-authority", mint]."""
    return find_program_address([POOL_AUTHORITY_SEED, b58decode32(mint)], PUMP_PROGRAM_ID)[0]


def canonical_pumpswap_pool(mint: str, quote_mint: str = WSOL_MINT) -> str:
    """Pool canónico de una migración: ["pool", index u16 = 0, pool_authority, mint, wsol] bajo PumpSwap."""
    seeds = [
        POOL_SEED,
        CANONICAL_POOL_INDEX.to_bytes(2, "little"),
        b58decode32(pump_pool_authority(mint)),
        b58decode32(mint),
        b58decode32(quote_mint),
    ]
    return find_program_address(seeds, PUMPSWAP_PROGRAM_ID)[0]
