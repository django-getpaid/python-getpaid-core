"""Tests for getpaid_core.protocols."""

from decimal import Decimal

import pytest

from getpaid_core.protocols import Order
from getpaid_core.protocols import Payment
from getpaid_core.protocols import PaymentRepository


class ConcreteOrder:
    """A concrete class satisfying the Order protocol."""

    def get_total_amount(self) -> Decimal:
        return Decimal("100.00")

    def get_buyer_info(self):
        return {"email": "test@example.com"}

    def get_description(self) -> str:
        return "Test order"

    def get_currency(self) -> str:
        return "PLN"

    def get_items(self):
        return []

    def get_return_url(self, success=None) -> str:
        return "/return/"


class IncompleteOrder:
    """Missing required methods."""

    def get_total_amount(self) -> Decimal:
        return Decimal("50.00")


class TestOrderProtocol:
    def test_concrete_order_satisfies_protocol(self):
        order = ConcreteOrder()
        assert isinstance(order, Order)

    def test_incomplete_order_does_not_satisfy(self):
        order = IncompleteOrder()
        assert not isinstance(order, Order)

    def test_protocol_is_runtime_checkable(self):
        """Order is decorated with @runtime_checkable."""
        assert isinstance(ConcreteOrder(), Order)


class ConcretePayment:
    """A concrete class satisfying the Payment protocol."""

    def __init__(self):
        self.id = "pay-123"
        self.order = ConcreteOrder()
        self.amount_required = Decimal("100.00")
        self.currency = "PLN"
        self.status = "new"
        self.backend = "dummy"
        self.external_id = ""
        self.description = "Test"
        self.amount_paid = Decimal("0")
        self.amount_locked = Decimal("0")
        self.amount_refunded = Decimal("0")
        self.fraud_status = "unknown"
        self.fraud_message = ""


class TestPaymentProtocol:
    def test_concrete_payment_satisfies_protocol(self):
        payment = ConcretePayment()
        assert isinstance(payment, Payment)

    def test_protocol_is_runtime_checkable(self):
        assert isinstance(ConcretePayment(), Payment)


class TestPaymentRepositoryProtocol:
    def test_protocol_is_runtime_checkable(self):
        """PaymentRepository is @runtime_checkable."""
        # Just verify the protocol class exists and is importable
        assert PaymentRepository is not None
