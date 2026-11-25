"""Cabeceras de seguridad, límite de consultas por ruta y autenticación de controles."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.settings import Settings

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58 de una clave pública de Solana


def validate_mint(mint: str) -> str:
    if not MINT_RE.fullmatch(mint):
        raise HTTPException(status_code=422, detail="mint inválido: se espera una dirección base58 de 32 a 44 caracteres")
    return mint


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Ventana deslizante de 60 s por cliente y ruta. Los health checks quedan exentos."""

    def __init__(self, app, per_minute: int) -> None:
        super().__init__(app)
        self.per_minute = per_minute
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next):
        if self.per_minute <= 0 or request.url.path.startswith("/health"):
            return await call_next(request)
        key = (request.client.host if request.client else "unknown", request.url.path)
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                retry = max(1, int(60 - (now - q[0])))
                return JSONResponse(
                    {"detail": "demasiadas consultas"}, status_code=429, headers={"Retry-After": str(retry)}
                )
            q.append(now)
            if len(self._hits) > 5000:  # evita crecimiento sin límite
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                    self._hits.pop(k, None)
        return await call_next(request)


def require_admin(settings_provider):
    """Dependencia: exige ``X-Admin-Token`` y rechaza si los controles no están habilitados."""

    def dep(x_admin_token: str | None = Header(default=None), settings: Settings = Depends(settings_provider)) -> None:
        expected = settings.admin_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=403, detail="controles deshabilitados: configurar ADMIN_TOKEN")
        if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
            raise HTTPException(status_code=401, detail="token de administración inválido")

    return dep
