"""Proveedor de órdenes falso con una «cadena» en memoria. Sin red, sin claves y sin dinero.

La cadena sobrevive a los reinicios del ejecutor, como la real: lo que se haya enviado queda registrado aunque el
proceso que lo envió muera.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

from app.application.execution.ports import ChainResult, ChainStatus, SignedOrder, SubmitOutcome
from app.domain.execution import Fill, OrderRequest, PreparedOrder

FEE = 105_000


class ProcessCrash(BaseException):
    """El proceso muere: no es un ``Exception``, así que nadie lo atrapa ni libera el reclamo."""


class FakeChain:
    def __init__(self) -> None:
        self.landed: dict[str, ChainResult] = {}
        self.orders: dict[str, PreparedOrder] = {}


class FakeGateway:
    name = "fake"
    wallet = "FakeWa11et111111111111111111111111111111111"

    def __init__(self, clock, chain: FakeChain | None = None) -> None:
        self.clock = clock
        self.chain = chain or FakeChain()
        self.balance = 2_000_000_000
        self.price: dict[str, int] = {}  # mint -> unidades de token por lamport (en milésimas)
        self.expected_out: int | None = None  # fija la salida de la próxima orden
        self.order_changes: dict = {}
        self.prepare_error: Exception | None = None
        # qué pasa en cada envío, en orden; agotada la lista: "land"
        self.submits: list[str] = []
        self.prepare_hook = None
        self.prepared: list[OrderRequest] = []
        self.signed: list[str] = []
        self.sent: list[str] = []
        self._lock = threading.Lock()
        self._requests: dict[str, OrderRequest] = {}

    def balance_lamports(self) -> int:
        return self.balance

    def prepare(self, request: OrderRequest) -> PreparedOrder:
        if self.prepare_hook:
            self.prepare_hook()
        if self.prepare_error:
            raise self.prepare_error
        with self._lock:
            self.prepared.append(request)
            number = len(self.prepared)
        assert self.expected_out is not None, "la prueba debe fijar cuánto devuelve la orden"
        out = self.expected_out
        fields = {
            "request_id": f"req-{id(self)}-{number}", "taker": request.wallet, "input_mint": request.input_mint,
            "output_mint": request.output_mint, "in_amount": request.amount, "out_amount": out,
            "min_out_amount": out * (10_000 - request.slippage_bps) // 10_000, "price_impact_bps": Decimal(20),
            "fee_lamports": FEE, "valid_until": self.clock.now() + timedelta(seconds=60),
        }  # fmt: skip
        order = PreparedOrder(**{**fields, **self.order_changes})
        self._requests[order.request_id] = request
        return order

    def sign(self, order: PreparedOrder) -> SignedOrder:
        signature = f"sig-{order.request_id}"
        with self._lock:
            self.signed.append(signature)
        self.chain.orders[signature] = order
        return SignedOrder(signature, f"payload|{signature}", order.valid_until)

    def submit(self, signed: SignedOrder) -> SubmitOutcome:
        with self._lock:
            self.sent.append(signed.signature)
            action = self.submits.pop(0) if self.submits else "land"
        if action == "crash_before":
            raise ProcessCrash
        if action in ("land", "crash_after", "timeout_landed"):
            self.land(signed.signature)
        if action == "crash_after":
            raise ProcessCrash
        if action in ("timeout_landed", "timeout_lost"):
            raise TimeoutError("sin respuesta")
        if action == "fail":
            self.chain.landed[signed.signature] = ChainResult(ChainStatus.FAILED, Fill(-5_000, 0, 5_000))
        return SubmitOutcome.SENT

    def land(self, signature: str) -> None:
        """La transacción entra en la cadena una sola vez, por más que se reenvíe."""
        if signature in self.chain.landed:
            return
        order = self.chain.orders[signature]
        buying = order.output_mint != "So11111111111111111111111111111111111111112"
        fill = (
            Fill(-(order.in_amount + order.fee_lamports), order.out_amount, order.fee_lamports)
            if buying
            else Fill(order.out_amount - order.fee_lamports, -order.in_amount, order.fee_lamports)
        )
        self.chain.landed[signature] = ChainResult(ChainStatus.CONFIRMED, fill)

    def lookup(self, signature: str) -> ChainResult:
        if signature in self.chain.landed:
            return self.chain.landed[signature]
        expired = self.clock.now() >= self.chain.orders[signature].valid_until
        return ChainResult(ChainStatus.EXPIRED if expired else ChainStatus.NOT_FOUND)
