"""Dummy payment backend for development and testing."""

from collections.abc import Sequence
from decimal import Context
from decimal import Decimal
from typing import ClassVar

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import PaymentEvent
from getpaid_core.processor import BaseProcessor
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult


def _fallback_event_id(family: str, payment_id: str, amount: Decimal) -> str:
    """Synthesize a provider event ID for a cumulative progress callback.

    ``family`` scopes the ID to one kind of progress (``"payment"`` or
    ``"refund"``), and the cumulative ``amount`` distinguishes the events
    within it. A payment-wide ID would make the core dedupe treat every
    update after the first as a replay and discard it.

    The amount is canonicalized, so callbacks reporting the same
    cumulative total in different notations (``"40"``, ``"40.00"``) name
    one event and the second is correctly deduped.
    """
    return f"{family}:{payment_id}:{_canonical_amount(amount)}"


def _canonical_amount(amount: Decimal) -> str:
    """Render ``amount`` as one lossless, context-independent string.

    ``Decimal.normalize()`` alone would strip trailing zeros *and* round
    to the active context precision, so two totals that differ only
    below that precision (``40.01`` and ``40.02`` under ``prec=3``) would
    collapse into one event ID and the later update would be discarded as
    a replay. Normalizing in a context sized to the value's own
    significant digits strips the trailing zeros without ever rounding,
    which keeps the ID both stable across Decimal contexts and distinct
    for every distinct total.
    """
    digits = len(amount.as_tuple().digits)
    canonical = amount.normalize(context=Context(prec=max(digits, 1)))
    return f"{canonical:f}"


class DummyProcessor(BaseProcessor):
    """Dummy processor that simulates all payment operations.

    Development and testing only -- it performs no callback
    authentication (see :meth:`verify_callback`). Never use it in
    production.
    """

    slug: ClassVar[str] = "dummy"
    display_name: ClassVar[str] = "Dummy"
    accepted_currencies: ClassVar[Sequence[str]] = (
        "PLN",
        "EUR",
        "USD",
        "GBP",
        "CHF",
        "CZK",
    )

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        method = BackendMethod(self.get_setting("method", BackendMethod.REST))
        if method is BackendMethod.POST:
            return TransactionResult(
                method=method,
                redirect_url="https://dummy.example.com/form",
                form_data={
                    "payment_id": self.payment.id,
                    "amount": f"{self.payment.amount_required:.2f}",
                    "currency": self.payment.currency,
                },
            )

        return TransactionResult(
            method=method,
            redirect_url=f"https://dummy.example.com/pay/{self.payment.id}",
        )

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Explicit no-op verification.

        This backend is for development and testing only -- it performs
        no real callback authentication. Never use it in production.
        """

    def _event_id(self, data: dict, family: str, amount: Decimal) -> str:
        """Resolve the provider event ID for a cumulative callback.

        A non-empty ``event_id`` supplied by the caller always wins; a
        blank or missing one falls back to the amount-keyed ID.
        """
        supplied = data.get("event_id")
        if supplied:
            return str(supplied)
        return _fallback_event_id(family, self.payment.id, amount)

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> PaymentUpdate | None:
        """Map a simulated callback payload to a semantic update.

        Callers may pass an explicit ``event_id`` to control dedupe. When
        it is omitted, distinct cumulative totals get distinct synthesized
        IDs (see :func:`_fallback_event_id`), so a staged 40 -> 100
        capture or refund progresses, while re-sending an identical
        payload stays a harmless replay.
        """
        event = data.get("event")
        if event == "payment_confirmed":
            amount = Decimal(
                str(data.get("paid_amount", self.payment.amount_required))
            )
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=amount,
                provider_event_id=self._event_id(data, "payment", amount),
            )
        if event == "payment_failed":
            return PaymentUpdate(payment_event=PaymentEvent.FAILED)
        if event == "payment_locked":
            amount = Decimal(
                str(data.get("locked_amount", self.payment.amount_required))
            )
            return PaymentUpdate(
                payment_event=PaymentEvent.LOCKED,
                locked_amount=amount,
            )
        if event == "refund_confirmed":
            amount = Decimal(
                str(data.get("refunded_amount", self.payment.amount_paid))
            )
            return PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=amount,
                provider_event_id=self._event_id(data, "refund", amount),
            )
        if event == "refund_cancelled":
            return PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED)
        if event == "fraud_review":
            return PaymentUpdate(
                fraud_event=FraudEvent.REVIEW,
                fraud_message="Manual review required",
            )
        if event == "fraud_rejected":
            return PaymentUpdate(
                fraud_event=FraudEvent.REJECT,
                fraud_message="Rejected by dummy backend",
            )
        if event == "fraud_accepted":
            return PaymentUpdate(
                fraud_event=FraudEvent.ACCEPT,
                fraud_message="Accepted by dummy backend",
            )
        return None

    async def fetch_payment_status(self, **kwargs) -> PaymentUpdate | None:
        event = self.get_setting("confirmation_event", "payment_confirmed")
        if event == "payment_locked":
            return PaymentUpdate(
                payment_event=PaymentEvent.LOCKED,
                locked_amount=self.payment.amount_required,
                provider_event_id=f"pull-lock:{self.payment.id}",
            )
        if event == "payment_failed":
            return PaymentUpdate(
                payment_event=PaymentEvent.FAILED,
                provider_event_id=f"pull-fail:{self.payment.id}",
            )
        return PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=self.payment.amount_required,
            provider_event_id=f"pull-pay:{self.payment.id}",
        )

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResult:
        charged = amount if amount is not None else self.payment.amount_required
        return ChargeResult(
            amount_charged=charged,
            success=True,
            async_call=bool(kwargs.get("async_call", False)),
        )

    async def release_lock(self, **kwargs) -> Decimal:
        return self.payment.amount_locked

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> RefundResult:
        return RefundResult(
            amount=amount if amount is not None else self.payment.amount_paid,
            provider_data={"refund_id": f"dummy-refund-{self.payment.id}"},
        )

    async def cancel_refund(self, **kwargs) -> bool:
        return True
