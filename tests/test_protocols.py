"""Tests for getpaid_core.protocols."""

from decimal import Decimal

from getpaid_core.protocols import Order
from getpaid_core.protocols import Payment
from getpaid_core.protocols import PaymentRepository


class ConcreteOrder:
    def get_total_amount(self) -> Decimal:
        return Decimal("100.00")

    def get_buyer_info(self) -> dict[str, str]:
        return {"email": "test@example.com"}

    def get_description(self) -> str:
        return "Test order"

    def get_currency(self) -> str:
        return "PLN"

    def get_items(self) -> list[dict]:
        return []

    def get_return_url(self, success: bool | None = None) -> str:
        return "/return/"


class ConcretePayment:
    def __init__(self) -> None:
        self.id = "pay-123"
        self.order = ConcreteOrder()
        self.amount_required = Decimal("100.00")
        self.currency = "PLN"
        self.status = "new"
        self.backend = "dummy"
        self.external_id = None
        self.description = "Test"
        self.amount_paid = Decimal("0")
        self.amount_locked = Decimal("0")
        self.amount_refunded = Decimal("0")
        self.fraud_status = "unknown"
        self.fraud_message = ""
        self.provider_data = {}

    def is_fully_paid(self) -> bool:
        return False

    def is_fully_refunded(self) -> bool:
        return False


class ConcreteRepository:
    async def get_by_id(self, payment_id: str) -> ConcretePayment:
        return ConcretePayment()

    async def create(self, **kwargs) -> ConcretePayment:
        return ConcretePayment()

    async def save(self, payment: ConcretePayment) -> ConcretePayment:
        return payment

    async def update_status(
        self, payment_id: str, status: str, **fields
    ) -> ConcretePayment:
        payment = ConcretePayment()
        payment.status = status
        for key, value in fields.items():
            setattr(payment, key, value)
        return payment

    async def list_by_order(self, order_id: str) -> list[ConcretePayment]:
        return []


class TestOrderProtocol:
    def test_runtime_checkable(self) -> None:
        assert isinstance(ConcreteOrder(), Order)


class TestPaymentProtocol:
    def test_requires_provider_data_and_helpers(self) -> None:
        assert isinstance(ConcretePayment(), Payment)


class TestPaymentRepositoryProtocol:
    def test_runtime_checkable(self) -> None:
        assert isinstance(ConcreteRepository(), PaymentRepository)
