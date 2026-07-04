"""Shared test fixtures for getpaid-core."""

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

import pytest

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Order as OrderProtocol
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import BuyerInfo
from getpaid_core.types import ChargeResult
from getpaid_core.types import ItemInfo
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult


class MockOrder:
    """A mock order satisfying the Order protocol."""

    def __init__(
        self,
        total: Decimal = Decimal("100.00"),
        currency: str = "PLN",
        order_id: str = "order-1",
    ) -> None:
        self.id = order_id
        self._total = total
        self._currency = currency

    def get_total_amount(self) -> Decimal:
        return self._total

    def get_buyer_info(self) -> BuyerInfo:
        return BuyerInfo(email="test@example.com")

    def get_description(self) -> str:
        return "Test order"

    def get_currency(self) -> str:
        return self._currency

    def get_items(self) -> list[ItemInfo]:
        return []

    def get_return_url(self, success: bool | None = None) -> str:
        return "/return/"


class MockPayment:
    """A mock payment satisfying the Payment protocol."""

    def __init__(self, **kwargs) -> None:
        self.id: str = kwargs.get("id", "pay-1")
        self.order: OrderProtocol = kwargs.get("order", MockOrder())
        self.amount_required: Decimal = kwargs.get(
            "amount_required", Decimal("100.00")
        )
        self.currency: str = kwargs.get("currency", "PLN")
        self.status: str = kwargs.get("status", PaymentStatus.NEW)
        self.backend: str = kwargs.get("backend", "mock")
        self.external_id: str | None = kwargs.get("external_id")
        self.description: str | None = kwargs.get("description", "Test")
        self.amount_paid: Decimal = kwargs.get("amount_paid", Decimal("0"))
        self.amount_locked: Decimal = kwargs.get("amount_locked", Decimal("0"))
        self.amount_refunded: Decimal = kwargs.get(
            "amount_refunded", Decimal("0")
        )
        self.fraud_status: str = kwargs.get("fraud_status", FraudStatus.UNKNOWN)
        self.fraud_message: str = kwargs.get("fraud_message", "")
        self.provider_data: dict = dict(kwargs.get("provider_data", {}))

    def is_fully_paid(self) -> bool:
        return self.amount_paid >= self.amount_required

    def is_fully_refunded(self) -> bool:
        return self.amount_refunded >= self.amount_paid


class MockProcessor(BaseProcessor):
    """A mock processor for testing PaymentFlow."""

    slug = "mock"
    display_name = "Mock"
    accepted_currencies: ClassVar[Sequence[str]] = ("PLN", "EUR")

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        customer_ip = kwargs.get("customer_ip", "127.0.0.1")
        return TransactionResult(
            method="GET",
            redirect_url="https://mock.example.com/pay",
            external_id=f"ext-{self.payment.id}",
            provider_data={"customer_ip": customer_ip},
        )

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Explicit test-only no-op verification."""

    async def handle_callback(
        self,
        data: dict,
        headers: dict,
        **kwargs,
    ) -> PaymentUpdate | None:
        event = data.get("event")
        if event == "payment_confirmed":
            amount = Decimal(
                str(data.get("paid_amount", self.payment.amount_required))
            )
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=amount,
                provider_event_id=str(data.get("event_id", "callback-1")),
            )
        if event == "fraud_review":
            return PaymentUpdate(
                fraud_event="review",
                fraud_message="Manual review required",
            )
        return None

    async def fetch_payment_status(self, **kwargs) -> PaymentUpdate | None:
        return PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=self.payment.amount_required,
            provider_event_id="pull-1",
        )

    async def charge(
        self,
        amount: Decimal | None = None,
        **kwargs,
    ) -> ChargeResult:
        charged = amount if amount is not None else self.payment.amount_required
        async_call = bool(kwargs.get("async_call", False))
        return ChargeResult(
            amount_charged=charged,
            success=True,
            async_call=async_call,
        )

    async def release_lock(self, **kwargs) -> Decimal:
        return self.payment.amount_locked

    async def start_refund(
        self,
        amount: Decimal | None = None,
        **kwargs,
    ) -> RefundResult:
        refund_amount = (
            amount if amount is not None else self.payment.amount_paid
        )
        return RefundResult(
            amount=refund_amount,
            provider_data={"refund_id": "refund-1"},
        )

    async def cancel_refund(self, **kwargs) -> bool:
        return True


class MockRepository:
    """In-memory repository for testing."""

    def __init__(self) -> None:
        self._payments: dict[str, MockPayment] = {}
        self.save_calls = 0

    async def get_by_id(self, payment_id: str) -> MockPayment:
        return self._payments[payment_id]

    async def create(self, **kwargs) -> MockPayment:
        payment = MockPayment(**kwargs)
        self._payments[payment.id] = payment
        return payment

    async def save(self, payment: MockPayment) -> MockPayment:
        self.save_calls += 1
        self._payments[payment.id] = payment
        return payment

    async def update_status(
        self,
        payment_id: str,
        status: str,
        **fields,
    ) -> MockPayment:
        payment = self._payments[payment_id]
        payment.status = status
        for key, value in fields.items():
            setattr(payment, key, value)
        return payment

    async def list_by_order(self, order_id: str) -> list[MockPayment]:
        return list(self._payments.values())


@pytest.fixture
def mock_registry() -> PluginRegistry:
    """A fresh registry with MockProcessor registered."""
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(MockProcessor)
    return registry


@pytest.fixture
def mock_repo() -> MockRepository:
    return MockRepository()
