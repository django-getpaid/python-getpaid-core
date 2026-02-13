"""End-to-end integration tests for getpaid-core.

Tests full payment lifecycle: create -> prepare -> callback -> paid.
Uses the dummy backend and in-memory repository.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import PaymentStatus
from getpaid_core.flow import PaymentFlow
from getpaid_core.registry import PluginRegistry
from tests.conftest import MockOrder
from tests.conftest import MockRepository


@pytest.fixture
def e2e_registry():
    reg = PluginRegistry()
    reg._discovered = True
    reg.register(DummyProcessor)
    return reg


@pytest.fixture
def e2e_flow(e2e_registry):
    repo = MockRepository()
    with patch("getpaid_core.flow.registry", e2e_registry):
        yield PaymentFlow(
            repository=repo,
            config={"dummy": {"method": "REST"}},
        )


class TestFullPaymentLifecycle:
    @pytest.mark.asyncio
    async def test_create_prepare_callback_paid(self, e2e_flow):
        """Full happy path: create -> prepare -> callback -> paid."""
        order = MockOrder(total=Decimal("250.00"), currency="PLN")

        # 1. Create payment
        payment = await e2e_flow.create_payment(order, "dummy")
        assert payment.status == PaymentStatus.NEW
        assert payment.amount_required == Decimal("250.00")

        # 2. Prepare (transitions to PREPARED)
        result = await e2e_flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED
        assert result["method"] == "REST"

        # 3. Callback: confirm_payment (-> PARTIAL)
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "confirm_payment"},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

        # 4. Set amount_paid and mark as paid
        payment.amount_paid = Decimal("250.00")
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "mark_as_paid"},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID


class TestRefundLifecycle:
    @pytest.mark.asyncio
    async def test_paid_refund_cycle(self, e2e_flow):
        """Paid -> start_refund -> confirm_refund -> refunded."""
        order = MockOrder(total=Decimal("100.00"))
        payment = await e2e_flow.create_payment(order, "dummy")

        # Get to PAID state
        await e2e_flow.prepare(payment)
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "confirm_payment"},
            headers={},
        )
        payment.amount_paid = Decimal("100.00")
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "mark_as_paid"},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID

        # Start refund
        refund_amount = await e2e_flow.start_refund(payment)
        assert refund_amount == Decimal("100.00")
        assert payment.status == PaymentStatus.REFUND_STARTED

        # Confirm refund (back to PARTIAL)
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "confirm_refund"},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

        # Mark as fully refunded
        payment.amount_refunded = Decimal("100.00")
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "mark_as_refunded"},
            headers={},
        )
        assert payment.status == PaymentStatus.REFUNDED


class TestPreAuthLifecycle:
    @pytest.mark.asyncio
    async def test_preauth_charge_pay(self, e2e_flow):
        """NEW -> PREPARED -> PRE_AUTH -> IN_CHARGE -> PARTIAL -> PAID."""
        order = MockOrder(total=Decimal("200.00"))
        payment = await e2e_flow.create_payment(order, "dummy")

        await e2e_flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED

        # Lock (pre-auth)
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "confirm_lock"},
            headers={},
        )
        assert payment.status == PaymentStatus.PRE_AUTH

        # Charge
        result = await e2e_flow.charge(payment)
        assert result["success"] is True
        assert payment.status == PaymentStatus.IN_CHARGE

        # Payment received
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "confirm_payment"},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

        payment.amount_paid = Decimal("200.00")
        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "mark_as_paid"},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID


class TestFailureLifecycle:
    @pytest.mark.asyncio
    async def test_prepared_to_failed(self, e2e_flow):
        order = MockOrder()
        payment = await e2e_flow.create_payment(order, "dummy")
        await e2e_flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED

        await e2e_flow.handle_callback(
            payment,
            data={"new_status": "fail"},
            headers={},
        )
        assert payment.status == PaymentStatus.FAILED


class TestPullFlow:
    @pytest.mark.asyncio
    async def test_fetch_and_update(self, e2e_flow):
        order = MockOrder()
        payment = await e2e_flow.create_payment(order, "dummy")
        await e2e_flow.prepare(payment)

        payment = await e2e_flow.fetch_and_update_status(payment)
        # Default confirmation_status is confirm_payment
        assert payment.status == PaymentStatus.PARTIAL
