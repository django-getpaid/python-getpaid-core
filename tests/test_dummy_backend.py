"""Tests for the built-in dummy payment backend."""

from decimal import Decimal
from typing import cast

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import BackendMethod
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Payment as PaymentProtocol
from tests.conftest import MockPayment


class TestDummyProcessorAttributes:
    def test_is_base_processor(self) -> None:
        assert issubclass(DummyProcessor, BaseProcessor)

    def test_accepted_currencies(self) -> None:
        assert "PLN" in DummyProcessor.accepted_currencies


class TestDummyPrepareTransaction:
    @pytest.mark.asyncio
    async def test_get_method(self) -> None:
        payment = MockPayment(backend="dummy")
        processor = DummyProcessor(
            cast("PaymentProtocol", payment),
            config={"method": "GET"},
        )

        result = await processor.prepare_transaction()

        assert result.method is BackendMethod.GET
        assert result.redirect_url == "https://dummy.example.com/pay/pay-1"

    @pytest.mark.asyncio
    async def test_post_method(self) -> None:
        payment = MockPayment(backend="dummy")
        processor = DummyProcessor(
            cast("PaymentProtocol", payment),
            config={"method": "POST"},
        )

        result = await processor.prepare_transaction()

        assert result.method is BackendMethod.POST
        assert result.form_data == {
            "payment_id": "pay-1",
            "amount": "100.00",
            "currency": "PLN",
        }


class TestDummyHandleCallback:
    @pytest.mark.asyncio
    async def test_payment_confirmed_maps_to_semantic_update(self) -> None:
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)
        processor = DummyProcessor(cast("PaymentProtocol", payment))

        update = await processor.handle_callback(
            data={"event": "payment_confirmed", "paid_amount": "100.00"},
            headers={},
        )

        assert update is not None
        assert update.payment_event is PaymentEvent.PAYMENT_CAPTURED
        assert update.paid_amount == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_fraud_review_maps_to_fraud_update(self) -> None:
        payment = MockPayment(backend="dummy")
        processor = DummyProcessor(cast("PaymentProtocol", payment))

        update = await processor.handle_callback(
            data={"event": "fraud_review"},
            headers={},
        )

        assert update is not None
        assert str(update.fraud_event) == "review"


class TestDummyFetchPaymentStatus:
    @pytest.mark.asyncio
    async def test_returns_payment_update(self) -> None:
        payment = MockPayment(backend="dummy")
        processor = DummyProcessor(
            cast("PaymentProtocol", payment),
            config={"confirmation_event": "payment_confirmed"},
        )

        update = await processor.fetch_payment_status()

        assert update is not None
        assert update.payment_event is PaymentEvent.PAYMENT_CAPTURED


class TestDummyCharge:
    @pytest.mark.asyncio
    async def test_charge_full(self) -> None:
        payment = MockPayment(
            backend="dummy", amount_required=Decimal("100.00")
        )
        processor = DummyProcessor(cast("PaymentProtocol", payment))

        result = await processor.charge()

        assert result.success is True
        assert result.amount_charged == Decimal("100.00")


class TestDummyRefunds:
    @pytest.mark.asyncio
    async def test_start_refund_uses_amount_paid(self) -> None:
        payment = MockPayment(backend="dummy", amount_paid=Decimal("100.00"))
        processor = DummyProcessor(cast("PaymentProtocol", payment))

        result = await processor.start_refund()

        assert result.amount == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_cancel_refund_returns_true(self) -> None:
        payment = MockPayment(backend="dummy")
        processor = DummyProcessor(cast("PaymentProtocol", payment))

        result = await processor.cancel_refund()

        assert result is True
