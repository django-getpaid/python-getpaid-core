"""Integration-style tests for getpaid-core orchestration."""

from decimal import Decimal

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import PaymentStatus
from getpaid_core.flow import PaymentFlow
from getpaid_core.registry import PluginRegistry
from tests.conftest import MockOrder
from tests.conftest import MockRepository


@pytest.fixture
def integration_flow():
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(DummyProcessor)
    return PaymentFlow(
        repository=MockRepository(),
        config={"dummy": {"method": "REST"}},
        registry=registry,
    )


class TestFullPaymentLifecycle:
    @pytest.mark.asyncio
    async def test_create_prepare_callback_paid(self, integration_flow) -> None:
        order = MockOrder(total=Decimal("250.00"), currency="PLN")

        payment = await integration_flow.create_payment(order, "dummy")
        assert payment.status == PaymentStatus.NEW

        result = await integration_flow.prepare(payment)
        assert result.method == "REST"
        assert payment.status == PaymentStatus.PREPARED

        await integration_flow.handle_callback(
            payment,
            data={
                "event": "payment_confirmed",
                "paid_amount": "250.00",
                "event_id": "evt-1",
            },
            headers={},
        )

        assert payment.status == PaymentStatus.PAID
        assert payment.amount_paid == Decimal("250.00")


class TestRefundLifecycle:
    @pytest.mark.asyncio
    async def test_paid_refund_cycle(self, integration_flow) -> None:
        order = MockOrder(total=Decimal("100.00"))
        payment = await integration_flow.create_payment(order, "dummy")
        await integration_flow.prepare(payment)
        await integration_flow.handle_callback(
            payment,
            data={"event": "payment_confirmed", "paid_amount": "100.00"},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID

        refund = await integration_flow.start_refund(payment)
        assert refund.amount == Decimal("100.00")
        assert payment.status == PaymentStatus.REFUND_STARTED


class TestPullFlow:
    @pytest.mark.asyncio
    async def test_fetch_and_update(self, integration_flow) -> None:
        payment = await integration_flow.create_payment(MockOrder(), "dummy")
        await integration_flow.prepare(payment)

        payment = await integration_flow.fetch_and_update_status(payment)

        assert payment.status == PaymentStatus.PAID
