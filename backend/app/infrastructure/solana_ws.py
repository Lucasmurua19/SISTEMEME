"""Suscripción WebSocket a los logs de la cuenta de migración.

Es solo una vía de baja latencia: encola un trabajo idempotente por firma. Si el socket se corta, el
worker completa el tramo perdido desde el cursor persistido por RPC (la deduplicación evita repetidos).
La URL del socket puede incluir una clave: no se registra.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

from websockets.sync.client import connect

log = logging.getLogger(__name__)


def extract_signature(message: str) -> str | None:
    """Firma de una notificación de logs de una migración exitosa; ``None`` si no corresponde."""
    try:
        data = json.loads(message)
    except ValueError:
        return None
    if data.get("method") != "logsNotification":
        return None
    value = ((data.get("params") or {}).get("result") or {}).get("value") or {}
    if value.get("err") is not None or not value.get("signature"):
        return None
    logs = value.get("logs") or []
    if logs and not any("Migrate" in line for line in logs):  # si los logs vienen truncados se deja pasar
        return None
    return str(value["signature"])


class MigrationLogListener(threading.Thread):
    def __init__(
        self,
        ws_url: str,
        account: str,
        commitment: str,
        on_signature: Callable[[str], None],
        stop: threading.Event,
        *,
        backoff_max_s: float = 60.0,
    ) -> None:
        super().__init__(name="migration-ws", daemon=True)
        self._url = ws_url
        self._account = account
        self._commitment = commitment
        self._on_signature = on_signature
        self._stop_event = stop  # ojo: ``Thread._stop`` es un método interno, no se puede pisar
        self._backoff_max = backoff_max_s

    def run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                with connect(self._url, open_timeout=10, max_size=2**22) as ws:
                    ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "logsSubscribe",
                                "params": [{"mentions": [self._account]}, {"commitment": self._commitment}],
                            }
                        )
                    )
                    log.info("WebSocket de migraciones conectado")
                    backoff = 1.0
                    while not self._stop_event.is_set():
                        try:
                            message = ws.recv(timeout=20)
                        except TimeoutError:
                            ws.ping()
                            continue
                        sig = extract_signature(message if isinstance(message, str) else message.decode())
                        if sig:
                            self._on_signature(sig)
            except Exception as exc:  # sin la URL en el mensaje
                log.warning("WebSocket caído (%s); se reintenta en %.0fs", type(exc).__name__, backoff)
            self._stop_event.wait(backoff)
            backoff = min(self._backoff_max, backoff * 2)
