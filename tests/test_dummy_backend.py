"""Tests for the built-in dummy payment backend."""

from decimal import Decimal

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import PaymentStatus
from getpaid_core.fsm import create_payment_machine
from getpaid_core.processor import BaseProcessor
from tests.conftest import MockPayment


class TestDummyProcessorAttributes:
    def test_is_base_processor(self):
        assert issubclass(DummyProcessor, BaseProcessor)

    def test_slug(self):
        assert DummyProcessor.slug == "dummy"

    def test_display_name(self):
        assert DummyProcessor.display_name == "Dummy"

    def test_accepted_currencies(self):
        # Dummy accepts all common currencies
        assert "PLN" in DummyProcessor.accepted_currencies
        assert "EUR" in DummyProcessor.accepted_currencies
        assert "USD" in DummyProcessor.accepted_currencies


class TestDummyPrepareTransaction:
    @pytest.mark.asyncio
    async def test_get_method(self):
        payment = MockPayment(backend="dummy")
        proc = DummyProcessor(payment, config={"method": "GET"})
        result = await proc.prepare_transaction()
        assert result["method"] == "GET"
        assert result["redirect_url"] is not None

    @pytest.mark.asyncio
    async def test_post_method(self):
        payment = MockPayment(backend="dummy")
        proc = DummyProcessor(payment, config={"method": "POST"})
        result = await proc.prepare_transaction()
        assert result["method"] == "POST"
        assert result["form_data"] is not None

    @pytest.mark.asyncio
    async def test_rest_method_default(self):
        payment = MockPayment(backend="dummy")
        proc = DummyProcessor(payment)
        result = await proc.prepare_transaction()
        assert result["method"] == "REST"


class TestDummyHandleCallback:
    @pytest.mark.asyncio
    async def test_confirm_payment(self):
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)
        create_payment_machine(payment)
        proc = DummyProcessor(payment)
        await proc.handle_callback(
            data={"new_status": "confirm_payment"},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_fail(self):
        payment = MockPayment(backend="dummy", status=PaymentStatus.NEW)
        create_payment_machine(payment)
        proc = DummyProcessor(payment)
        await proc.handle_callback(data={"new_status": "fail"}, headers={})
        assert payment.status == PaymentStatus.FAILED


class TestDummyFetchPaymentStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self):
        payment = MockPayment(backend="dummy")
        proc = DummyProcessor(
            payment,
            config={"confirmation_status": "confirm_payment"},
        )
        result = await proc.fetch_payment_status()
        assert result["status"] == "confirm_payment"


class TestDummyCharge:
    @pytest.mark.asyncio
    async def test_charge_full(self):
        payment = MockPayment(
            backend="dummy",
            amount_required=Decimal("100"),
        )
        proc = DummyProcessor(payment)
        result = await proc.charge()
        assert result["success"] is True
        assert result["amount_charged"] == Decimal("100")

    @pytest.mark.asyncio
    async def test_charge_partial(self):
        payment = MockPayment(
            backend="dummy",
            amount_required=Decimal("100"),
        )
        proc = DummyProcessor(payment)
        result = await proc.charge(amount=Decimal("50"))
        assert result["amount_charged"] == Decimal("50")


class TestDummyReleaseLock:
    @pytest.mark.asyncio
    async def test_returns_locked_amount(self):
        payment = MockPayment(
            backend="dummy",
            amount_locked=Decimal("100"),
        )
        proc = DummyProcessor(payment)
        amount = await proc.release_lock()
        assert amount == Decimal("100")


class TestDummyStartRefund:
    @pytest.mark.asyncio
    async def test_refund_full(self):
        payment = MockPayment(
            backend="dummy",
            amount_paid=Decimal("100"),
        )
        proc = DummyProcessor(payment)
        amount = await proc.start_refund()
        assert amount == Decimal("100")

    @pytest.mark.asyncio
    async def test_refund_partial(self):
        payment = MockPayment(
            backend="dummy",
            amount_paid=Decimal("100"),
        )
        proc = DummyProcessor(payment)
        amount = await proc.start_refund(amount=Decimal("30"))
        assert amount == Decimal("30")


class TestDummyCancelRefund:
    @pytest.mark.asyncio
    async def test_returns_true(self):
        payment = MockPayment(backend="dummy")
        proc = DummyProcessor(payment)
        result = await proc.cancel_refund()
        assert result is True
