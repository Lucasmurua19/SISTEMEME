"""Logging con redacción de secretos (URLs RPC con claves, cabeceras de API)."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"(api[-_]?key=)[^&\s\"']+", re.I), r"\1***"),
    (re.compile(r"(x-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\"]+", re.I), r"\1***"),
    (re.compile(r"(https?|wss?)://[^\s/@]+@", re.I), r"\1://***@"),  # credenciales en la URL
    (re.compile(r"(https?://[^\s/]+/)[A-Za-z0-9_\-]{24,}"), r"\1***"),  # token en el path (p. ej. proveedores RPC)
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.getMessage()))
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx registra la URL completa de cada llamada
