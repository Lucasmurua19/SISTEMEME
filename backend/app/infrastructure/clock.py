"""Relojes: sistema, manual (pruebas) y de reproducción (fixtures)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Reloj controlado: permite reproducir cálculos con instantes exactos."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("el reloj requiere zona horaria UTC")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


class ReplayClock(ManualClock):
    """Reloj de reproducción: avanza por pasos fijos y nunca supera el reloj real.

    Arranca en ``start`` (anterior al presente) y alcanza el tiempo real al terminar la reproducción;
    a partir de ahí sigue en tiempo real. Así nunca hay instantes en el futuro.
    """

    def __init__(self, start: datetime, step_s: int, real: SystemClock | None = None) -> None:
        super().__init__(start)
        self._step = step_s
        self._real = real or SystemClock()

    def tick(self) -> datetime:
        real_now = self._real.now()
        nxt = self._now + timedelta(seconds=self._step)
        self._now = max(self._now, min(nxt, real_now))  # nunca retrocede ni pasa al futuro
        return self._now

    @property
    def caught_up(self) -> bool:
        return self._now >= self._real.now() - timedelta(seconds=self._step)
