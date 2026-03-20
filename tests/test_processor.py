"""Tests for getpaid_core.processor.BaseProcessor."""

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar
from typing import cast

import pytest

from getpaid_core.enums import BackendMethod
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Order as OrderProtocol
from getpaid_core.protocols import Payment as PaymentProtocol
from getpaid_core.types import BuyerInfo
from getpaid_core.types import ItemInfo
from getpaid_core.types import TransactionResult


class ConcreteOrder:
    def get_total_amount(self) -> Decimal:
        return Decimal("100.00")

    def get_buyer_info(self) -> BuyerInfo:
        return BuyerInfo(email="test@example.com")

    def get_description(self) -> str:
        return "Test"

    def get_currency(self) -> str:
        return "PLN"

    def get_items(self) -> list[ItemInfo]:
        return []

    def get_return_url(self, success: bool | None = None) -> str:
        return "/return/"


class ConcretePayment:
    def __init__(self) -> None:
        self.id: str = "pay-1"
        self.order: OrderProtocol = ConcreteOrder()
        self.amount_required: Decimal = Decimal("100.00")
        self.currency: str = "PLN"
        self.status: str = "new"
        self.backend: str = "test"
        self.external_id: str | None = None
        self.description: str | None = "Test"
        self.amount_paid: Decimal = Decimal("0")
        self.amount_locked: Decimal = Decimal("0")
        self.amount_refunded: Decimal = Decimal("0")
        self.fraud_status: str = "unknown"
        self.fraud_message: str = ""
        self.provider_data: dict = {}

    def is_fully_paid(self) -> bool:
        return False

    def is_fully_refunded(self) -> bool:
        return False


class DummyProcessor(BaseProcessor):
    slug = "dummy"
    display_name = "Dummy"
    accepted_currencies: ClassVar[Sequence[str]] = ("PLN", "EUR")
    sandbox_url = "https://sandbox.example.com"
    production_url = "https://api.example.com"

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(
            method=BackendMethod.GET,
            redirect_url="https://sandbox.example.com/pay",
        )


class TestBaseProcessorCannotInstantiate:
    def test_abstract(self) -> None:
        processor_class = cast("type[BaseProcessor]", BaseProcessor)
        with pytest.raises(TypeError):
            processor_class(cast("PaymentProtocol", ConcretePayment()))


class TestBaseProcessorInit:
    def test_payment_stored(self) -> None:
        processor = DummyProcessor(cast("PaymentProtocol", ConcretePayment()))
        assert processor.payment.id == "pay-1"

    def test_config_default_empty(self) -> None:
        processor = DummyProcessor(cast("PaymentProtocol", ConcretePayment()))
        assert processor.config == {}


class TestGetPaywallBaseurl:
    def test_sandbox_by_default(self) -> None:
        processor = DummyProcessor(cast("PaymentProtocol", ConcretePayment()))
        assert processor.get_paywall_baseurl() == "https://sandbox.example.com"

    def test_production(self) -> None:
        processor = DummyProcessor(
            cast("PaymentProtocol", ConcretePayment()),
            config={"sandbox": False},
        )
        assert processor.get_paywall_baseurl() == "https://api.example.com"


class TestPrepareTransaction:
    @pytest.mark.asyncio
    async def test_returns_transaction_result(self) -> None:
        processor = DummyProcessor(cast("PaymentProtocol", ConcretePayment()))

        result = await processor.prepare_transaction()

        assert result.method is BackendMethod.GET
        assert result.redirect_url == "https://sandbox.example.com/pay"


class TestOptionalMethodsRaiseNotImplemented:
    @pytest.mark.asyncio
    async def test_handle_callback(self) -> None:
        processor = DummyProcessor(cast("PaymentProtocol", ConcretePayment()))
        with pytest.raises(NotImplementedError):
            await processor.handle_callback({}, {})

    @pytest.mark.asyncio
    async def test_fetch_payment_status(self) -> None:
        processor = DummyProcessor(cast("PaymentProtocol", ConcretePayment()))
        with pytest.raises(NotImplementedError):
            await processor.fetch_payment_status()
