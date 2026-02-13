"""Dummy payment backend for development and testing.

Makes zero HTTP calls. Serves as a reference implementation
for backend authors.
"""

from decimal import Decimal
from typing import ClassVar

from getpaid_core.fsm import ALLOWED_CALLBACKS
from getpaid_core.processor import BaseProcessor
from getpaid_core.types import ChargeResponse
from getpaid_core.types import PaymentStatusResponse
from getpaid_core.types import TransactionResult


class DummyProcessor(BaseProcessor):
    """Dummy processor that simulates all payment operations."""

    slug: ClassVar[str] = "dummy"
    display_name: ClassVar[str] = "Dummy"
    accepted_currencies: ClassVar[list[str]] = [
        "PLN",
        "EUR",
        "USD",
        "GBP",
        "CHF",
        "CZK",
    ]

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        method = self.get_setting("method", "REST")
        if method == "POST":
            return TransactionResult(
                redirect_url="https://dummy.example.com/form",
                form_data={
                    "payment_id": self.payment.id,
                    "amount": str(self.payment.amount_required),
                    "currency": self.payment.currency,
                },
                method="POST",
                headers={},
            )
        elif method == "GET":
            return TransactionResult(
                redirect_url=(
                    f"https://dummy.example.com/pay/{self.payment.id}"
                ),
                form_data=None,
                method="GET",
                headers={},
            )
        else:
            return TransactionResult(
                redirect_url=(
                    f"https://dummy.example.com/pay/{self.payment.id}"
                ),
                form_data=None,
                method="REST",
                headers={},
            )

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        new_status = data.get("new_status")
        if new_status and new_status in ALLOWED_CALLBACKS:
            trigger = getattr(self.payment, new_status, None)
            if trigger and callable(trigger):
                trigger()

    async def fetch_payment_status(self, **kwargs) -> PaymentStatusResponse:
        status = self.get_setting("confirmation_status", "confirm_payment")
        return PaymentStatusResponse(status=status)

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResponse:
        charged = amount if amount is not None else self.payment.amount_required
        return ChargeResponse(
            amount_charged=charged,
            success=True,
            async_call=False,
        )

    async def release_lock(self, **kwargs) -> Decimal:
        return self.payment.amount_locked

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> Decimal:
        return amount if amount is not None else self.payment.amount_paid

    async def cancel_refund(self, **kwargs) -> bool:
        return True
