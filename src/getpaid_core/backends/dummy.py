"""Dummy payment backend for development and testing."""

from collections.abc import Sequence
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


class DummyProcessor(BaseProcessor):
    """Dummy processor that simulates all payment operations."""

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

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> PaymentUpdate | None:
        event = data.get("event")
        if event == "payment_confirmed":
            amount = Decimal(
                str(data.get("paid_amount", self.payment.amount_required))
            )
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=amount,
                provider_event_id=str(
                    data.get("event_id", f"payment:{self.payment.id}")
                ),
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
                provider_event_id=str(
                    data.get("event_id", f"refund:{self.payment.id}")
                ),
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
