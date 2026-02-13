"""Tests for getpaid_core.processor.BaseProcessor."""

from decimal import Decimal

import pytest

from getpaid_core.processor import BaseProcessor
from getpaid_core.types import TransactionResult


# -- Test fixtures --


class ConcreteOrder:
    def get_total_amount(self):
        return Decimal("100.00")

    def get_buyer_info(self):
        return {"email": "test@example.com"}

    def get_description(self):
        return "Test"

    def get_currency(self):
        return "PLN"

    def get_items(self):
        return []

    def get_return_url(self, success=None):
        return "/return/"


class ConcretePayment:
    def __init__(self):
        self.id = "pay-1"
        self.order = ConcreteOrder()
        self.amount_required = Decimal("100.00")
        self.currency = "PLN"
        self.status = "new"
        self.backend = "test"
        self.external_id = ""
        self.description = "Test"
        self.amount_paid = Decimal("0")
        self.amount_locked = Decimal("0")
        self.amount_refunded = Decimal("0")
        self.fraud_status = "unknown"
        self.fraud_message = ""


class DummyProcessor(BaseProcessor):
    slug = "dummy"
    display_name = "Dummy"
    accepted_currencies = ["PLN", "EUR"]
    sandbox_url = "https://sandbox.example.com"
    production_url = "https://api.example.com"

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(
            redirect_url="https://sandbox.example.com/pay",
            form_data=None,
            method="GET",
            headers={},
        )


# -- Tests --


class TestBaseProcessorCannotInstantiate:
    def test_abstract(self):
        """BaseProcessor cannot be instantiated directly."""
        with pytest.raises(TypeError):
            BaseProcessor(ConcretePayment())


class TestBaseProcessorInit:
    def test_payment_stored(self):
        p = DummyProcessor(ConcretePayment())
        assert p.payment.id == "pay-1"

    def test_config_default_empty(self):
        p = DummyProcessor(ConcretePayment())
        assert p.config == {}

    def test_config_provided(self):
        cfg = {"api_key": "secret"}
        p = DummyProcessor(ConcretePayment(), config=cfg)
        assert p.config == cfg


class TestGetSetting:
    def test_returns_value(self):
        p = DummyProcessor(ConcretePayment(), config={"api_key": "abc"})
        assert p.get_setting("api_key") == "abc"

    def test_returns_default_when_missing(self):
        p = DummyProcessor(ConcretePayment())
        assert p.get_setting("missing", "fallback") == "fallback"

    def test_returns_none_when_missing_no_default(self):
        p = DummyProcessor(ConcretePayment())
        assert p.get_setting("missing") is None


class TestGetPaywallBaseurl:
    def test_sandbox_by_default(self):
        p = DummyProcessor(ConcretePayment())
        assert p.get_paywall_baseurl() == "https://sandbox.example.com"

    def test_sandbox_explicit(self):
        p = DummyProcessor(ConcretePayment(), config={"sandbox": True})
        assert p.get_paywall_baseurl() == "https://sandbox.example.com"

    def test_production(self):
        p = DummyProcessor(ConcretePayment(), config={"sandbox": False})
        assert p.get_paywall_baseurl() == "https://api.example.com"


class TestClassAttributes:
    def test_slug(self):
        assert DummyProcessor.slug == "dummy"

    def test_display_name(self):
        assert DummyProcessor.display_name == "Dummy"

    def test_accepted_currencies(self):
        assert DummyProcessor.accepted_currencies == ["PLN", "EUR"]


class TestPrepareTransaction:
    @pytest.mark.asyncio
    async def test_returns_transaction_result(self):
        p = DummyProcessor(ConcretePayment())
        result = await p.prepare_transaction()
        assert result["method"] == "GET"
        assert result["redirect_url"] == "https://sandbox.example.com/pay"


class TestOptionalMethodsRaiseNotImplemented:
    @pytest.mark.asyncio
    async def test_handle_callback(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.handle_callback({}, {})

    @pytest.mark.asyncio
    async def test_fetch_payment_status(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.fetch_payment_status()

    @pytest.mark.asyncio
    async def test_charge(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.charge()

    @pytest.mark.asyncio
    async def test_release_lock(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.release_lock()

    @pytest.mark.asyncio
    async def test_start_refund(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.start_refund()

    @pytest.mark.asyncio
    async def test_cancel_refund(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.cancel_refund()


class TestVerifyCallbackDefault:
    @pytest.mark.asyncio
    async def test_default_is_noop(self):
        """Default verify_callback does nothing (no-op)."""
        p = DummyProcessor(ConcretePayment())
        result = await p.verify_callback({}, {})
        assert result is None
