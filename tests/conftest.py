"""Shared test fixtures for getpaid-core."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import TransactionResult


class MockOrder:
    """A mock order satisfying the Order protocol."""

    def __init__(self, total=Decimal("100.00"), currency="PLN"):
        self._total = total
        self._currency = currency

    def get_total_amount(self):
        return self._total

    def get_buyer_info(self):
        return {"email": "test@example.com"}

    def get_description(self):
        return "Test order"

    def get_currency(self):
        return self._currency

    def get_items(self):
        return []

    def get_return_url(self, success=None):
        return "/return/"


class MockPayment:
    """A mock payment satisfying the Payment protocol."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "pay-1")
        self.order = kwargs.get("order", MockOrder())
        self.amount_required = kwargs.get("amount_required", Decimal("100.00"))
        self.currency = kwargs.get("currency", "PLN")
        self.status = kwargs.get("status", PaymentStatus.NEW)
        self.backend = kwargs.get("backend", "mock")
        self.external_id = kwargs.get("external_id", "")
        self.description = kwargs.get("description", "Test")
        self.amount_paid = kwargs.get("amount_paid", Decimal("0"))
        self.amount_locked = kwargs.get("amount_locked", Decimal("0"))
        self.amount_refunded = kwargs.get("amount_refunded", Decimal("0"))
        self.fraud_status = kwargs.get("fraud_status", FraudStatus.UNKNOWN)
        self.fraud_message = kwargs.get("fraud_message", "")

    def is_fully_paid(self):
        return self.amount_paid >= self.amount_required

    def is_fully_refunded(self):
        return self.amount_refunded >= self.amount_paid


class MockProcessor(BaseProcessor):
    """A mock processor for testing PaymentFlow."""

    slug = "mock"
    display_name = "Mock"
    accepted_currencies = ["PLN", "EUR"]

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url="https://mock.example.com/pay",
            form_data=None,
            method="GET",
            headers={},
        )

    async def handle_callback(self, data, headers, **kwargs):
        """Apply the status from callback data."""
        status = data.get("status")
        if status and hasattr(self.payment, status):
            trigger = getattr(self.payment, status)
            if callable(trigger):
                trigger()

    async def fetch_payment_status(self, **kwargs):
        return {"status": "confirm_payment"}


class MockRepository:
    """In-memory repository for testing."""

    def __init__(self):
        self._payments = {}

    async def get_by_id(self, payment_id):
        return self._payments[payment_id]

    async def create(self, **kwargs):
        payment = MockPayment(**kwargs)
        self._payments[payment.id] = payment
        return payment

    async def save(self, payment):
        self._payments[payment.id] = payment
        return payment

    async def update_status(self, payment_id, status, **fields):
        payment = self._payments[payment_id]
        payment.status = status
        for k, v in fields.items():
            setattr(payment, k, v)
        return payment

    async def list_by_order(self, order_id):
        return list(self._payments.values())


@pytest.fixture
def mock_registry():
    """A fresh registry with MockProcessor registered."""
    reg = PluginRegistry()
    reg._discovered = True
    reg.register(MockProcessor)
    return reg


@pytest.fixture
def mock_repo():
    return MockRepository()
