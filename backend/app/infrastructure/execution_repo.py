"""Persistencia de intenciones, intentos, posiciones y asientos de ejecución."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.application.execution.ports import ExecutionAttemptInfo, IntentInfo, LivePositionInfo
from app.domain.execution import OPEN_STATES, AttemptState, IntentStatus, Side, WalletState, check_transition
from app.infrastructure import orm
from app.infrastructure.repositories import insert_ignore

_OPEN = [s.value for s in OPEN_STATES]
_STAMPS = {
    AttemptState.PREPARED: "prepared_at",
    AttemptState.SIGNED: "signed_at",
    AttemptState.SUBMITTED: "submitted_at",
    AttemptState.CONFIRMED: "resolved_at",
    AttemptState.REJECTED: "resolved_at",
    AttemptState.FAILED: "resolved_at",
    AttemptState.EXPIRED: "resolved_at",
}


def _intent(r: orm.ExecutionIntent) -> IntentInfo:
    return IntentInfo(
        id=r.id, wallet=r.wallet, side=Side(r.side), signal_id=r.signal_id, position_id=r.position_id, pool=r.pool,
        mint=r.mint, amount=r.amount, strategy_version_id=r.strategy_version_id,
        policy=r.policy, trigger=r.trigger, status=r.status, reason=r.reason, failures=r.failures,
        expires_at=r.expires_at, next_attempt_at=r.next_attempt_at, claim_token=r.claim_token,
    )  # fmt: skip


def _attempt(r: orm.ExecutionAttempt) -> ExecutionAttemptInfo:
    return ExecutionAttemptInfo(
        id=r.id, intent_id=r.intent_id, number=r.number, state=AttemptState(r.state), signature=r.signature,
        signed_payload=r.signed_payload, valid_until=r.valid_until, min_out_amount=r.min_out_amount,
    )  # fmt: skip


def _position(r: orm.LivePosition) -> LivePositionInfo:
    return LivePositionInfo(
        id=r.id, wallet=r.wallet, buy_intent_id=r.buy_intent_id, signal_id=r.signal_id, pool=r.pool, mint=r.mint,
        strategy_version_id=r.strategy_version_id, status=r.status, tokens_acquired=r.tokens_acquired,
        tokens_open=r.tokens_open, sol_debited_lamports=r.sol_debited_lamports,
        sol_received_lamports=r.sol_received_lamports, pnl_lamports=r.pnl_lamports,
    )  # fmt: skip


class ExecutionRepo:
    def __init__(self, s: Session) -> None:
        self.s = s

    # ------------------------------------------------------------------ billetera
    def lock_wallet(self, wallet: str, now: datetime) -> bool:
        insert_ignore(
            self.s, orm.ExecutionWallet, {"wallet": wallet, "buys_paused": False, "updated_at": now},
            orm.ExecutionWallet.wallet,
        )  # fmt: skip
        stmt = select(orm.ExecutionWallet).where(orm.ExecutionWallet.wallet == wallet).with_for_update()
        row = self.s.scalar(stmt.execution_options(populate_existing=True))
        assert row is not None
        return row.buys_paused

    def pause_buys(self, wallet: str, reason: str, now: datetime) -> None:
        self.lock_wallet(wallet, now)
        row = self.s.get(orm.ExecutionWallet, wallet)
        assert row is not None
        row.buys_paused, row.pause_reason, row.updated_at = True, reason[:200], now

    def wallet_state(self, wallet: str, balance_lamports: int) -> WalletState:
        row = self.s.get(orm.ExecutionWallet, wallet)
        ledger = list(self.s.scalars(select(orm.LiveLedger).where(orm.LiveLedger.wallet == wallet)))
        reserved = sum(e.amount_lamports for e in ledger if e.kind == "reserve") - sum(
            e.amount_lamports for e in ledger if e.kind == "release"
        )
        positions = list(self.s.scalars(select(orm.LivePosition).where(orm.LivePosition.wallet == wallet)))
        closed = {p.id for p in positions if p.status == "closed"}
        realized = sum(p.pnl_lamports or 0 for p in positions if p.id in closed)
        loose_fees = sum(e.amount_lamports for e in ledger if e.kind == "fee" and e.position_id not in closed)
        # lo abierto cuenta a recuperación cero mientras no tenga una valoración
        deficit = sum(
            max(0, p.sol_debited_lamports - p.sol_received_lamports - (p.recoverable_lamports or 0))
            for p in positions
            if p.status == "open"
        )
        active_buys = self.s.scalar(
            select(func.count()).select_from(orm.ExecutionIntent).where(
                orm.ExecutionIntent.wallet == wallet, orm.ExecutionIntent.side == Side.BUY.value,
                orm.ExecutionIntent.status == IntentStatus.ACTIVE.value,
            )
        )  # fmt: skip
        return WalletState(
            balance_lamports=balance_lamports,
            reserved_lamports=reserved,
            open_positions=sum(1 for p in positions if p.status == "open"),
            active_buys=active_buys or 0,
            loss_lamports=max(0, deficit + loose_fees - realized),
            buys_paused=bool(row and row.buys_paused),
        )

    # ------------------------------------------------------------------ intenciones
    def create_intent(self, **fields: Any) -> int | None:
        values = {"failures": 0, "claim_token": 0, **fields}
        return insert_ignore(self.s, orm.ExecutionIntent, values, orm.ExecutionIntent.id)

    def _row(self, intent_id: int, lock: bool) -> orm.ExecutionIntent | None:
        stmt = select(orm.ExecutionIntent).where(orm.ExecutionIntent.id == intent_id)
        if lock:
            stmt = stmt.with_for_update()
        return self.s.scalar(stmt.execution_options(populate_existing=True))

    def get_intent(self, intent_id: int, *, lock: bool = False) -> IntentInfo | None:
        row = self._row(intent_id, lock)
        return _intent(row) if row else None

    def intent_by_key(self, key: str) -> IntentInfo | None:
        row = self.s.scalar(select(orm.ExecutionIntent).where(orm.ExecutionIntent.idempotency_key == key))
        return _intent(row) if row else None

    def blocking_sell(self, position_id: int) -> IntentInfo | None:
        row = self.s.scalar(
            select(orm.ExecutionIntent).where(
                orm.ExecutionIntent.position_id == position_id, orm.ExecutionIntent.side == Side.SELL.value,
                orm.ExecutionIntent.status.in_([IntentStatus.ACTIVE.value, IntentStatus.NEEDS_ATTENTION.value]),
            )
        )  # fmt: skip
        return _intent(row) if row else None

    def sell_count(self, position_id: int) -> int:
        return self.s.scalar(
            select(func.count()).select_from(orm.ExecutionIntent).where(orm.ExecutionIntent.position_id == position_id)
        ) or 0  # fmt: skip

    def due_intents(self, wallet: str, now: datetime, limit: int) -> list[int]:
        i = orm.ExecutionIntent
        return list(self.s.scalars(
            select(i.id).where(
                i.wallet == wallet, i.status == IntentStatus.ACTIVE.value,
                or_(i.next_attempt_at.is_(None), i.next_attempt_at <= now),
                or_(i.claim_expires_at.is_(None), i.claim_expires_at <= now),
            )
            .order_by(i.side.desc(), i.id)  # primero las ventas
            .limit(limit)
        ))  # fmt: skip

    def claim(self, intent_id: int, worker_id: str, now: datetime, ttl_s: int) -> IntentInfo | None:
        row = self._row(intent_id, lock=True)
        if row is None or row.status != IntentStatus.ACTIVE.value:
            return None
        if row.claim_expires_at is not None and row.claim_expires_at > now:
            return None  # otro avance en curso, aunque sea del mismo worker
        row.claim_token += 1
        row.claimed_by = worker_id
        row.claim_expires_at = now + timedelta(seconds=ttl_s)
        row.updated_at = now
        self.s.flush()
        return _intent(row)

    def release_claim(self, intent_id: int, token: int, now: datetime) -> None:
        row = self._row(intent_id, lock=True)
        if row is not None and row.claim_token == token:
            row.claimed_by, row.claim_expires_at, row.updated_at = None, None, now

    def update_intent(self, intent_id: int, now: datetime, **fields: Any) -> None:
        row = self.s.get(orm.ExecutionIntent, intent_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = now
        self.s.flush()

    # ------------------------------------------------------------------ intentos
    def open_attempt(self, intent_id: int) -> ExecutionAttemptInfo | None:
        row = self.s.scalar(
            select(orm.ExecutionAttempt).where(
                orm.ExecutionAttempt.intent_id == intent_id, orm.ExecutionAttempt.state.in_(_OPEN)
            )
        )
        return _attempt(row) if row else None

    def create_attempt(self, intent_id: int, gateway: str, now: datetime) -> ExecutionAttemptInfo:
        last = self.s.scalar(
            select(func.max(orm.ExecutionAttempt.number)).where(orm.ExecutionAttempt.intent_id == intent_id)
        )
        row = orm.ExecutionAttempt(
            intent_id=intent_id, number=(last or 0) + 1, state=AttemptState.PREPARING.value, gateway=gateway,
            created_at=now,
        )  # fmt: skip
        self.s.add(row)
        self.s.flush()  # el índice único impide un segundo intento abierto
        return _attempt(row)

    def get_attempt(self, attempt_id: int, *, lock: bool = False) -> ExecutionAttemptInfo | None:
        stmt = select(orm.ExecutionAttempt).where(orm.ExecutionAttempt.id == attempt_id)
        if lock:
            stmt = stmt.with_for_update()
        row = self.s.scalar(stmt)
        return _attempt(row) if row else None

    def move_attempt(self, attempt_id: int, state: AttemptState, now: datetime, **fields: Any) -> None:
        row = self.s.scalar(
            select(orm.ExecutionAttempt).where(orm.ExecutionAttempt.id == attempt_id).with_for_update()
        )
        assert row is not None
        check_transition(AttemptState(row.state), state)
        row.state = state.value
        stamp = _STAMPS.get(state)
        if stamp:
            setattr(row, stamp, now)
        for name, value in fields.items():
            setattr(row, name, value)
        self.s.flush()

    def failed_fees(self, position_id: int) -> int:
        entries = self.s.scalars(
            select(orm.LiveLedger).where(orm.LiveLedger.position_id == position_id, orm.LiveLedger.kind == "fee")
        )
        return sum(e.amount_lamports for e in entries)

    # ------------------------------------------------------------------ posiciones y asientos
    def create_position(self, **fields: Any) -> int:
        row = orm.LivePosition(**fields)
        self.s.add(row)
        self.s.flush()
        return row.id

    def get_position(self, position_id: int, *, lock: bool = False) -> LivePositionInfo | None:
        stmt = select(orm.LivePosition).where(orm.LivePosition.id == position_id)
        if lock:
            stmt = stmt.with_for_update()
        row = self.s.scalar(stmt.execution_options(populate_existing=True))
        return _position(row) if row else None

    def update_position(self, position_id: int, **fields: Any) -> None:
        row = self.s.get(orm.LivePosition, position_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        self.s.flush()

    def add_ledger(self, **fields: Any) -> bool:
        return insert_ignore(self.s, orm.LiveLedger, fields, orm.LiveLedger.id) is not None
