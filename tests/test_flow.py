"""Tests for getpaid_core.flow.PaymentFlow."""

from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.flow import PaymentFlow
from tests.conftest import MockOrder
from tests.conftest import MockPayment
from tests.conftest import MockProcessor


@pytest.fixture
def flow(mock_repo, mock_registry):
    """PaymentFlow with mock repo and registry."""
    with patch("getpaid_core.flow.registry", mock_registry):
        yield PaymentFlow(
            repository=mock_repo,
            config={"mock": {"sandbox": True}},
        )


class TestCreatePayment:
    @pytest.mark.asyncio
    async def test_creates_payment(self, flow, mock_repo):
        order = MockOrder()
        payment = await flow.create_payment(order, "mock")
        assert payment.backend == "mock"
        assert payment.amount_required == Decimal("100.00")
        assert payment.currency == "PLN"

    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self, flow):
        order = MockOrder()
        with pytest.raises(KeyError):
            await flow.create_payment(order, "nonexistent")


class TestPrepare:
    @pytest.mark.asyncio
    async def test_prepare_returns_transaction_result(self, flow):
        payment = MockPayment(backend="mock")
        result = await flow.prepare(payment)
        assert result["method"] == "GET"
        assert result["redirect_url"] == ("https://mock.example.com/pay")

    @pytest.mark.asyncio
    async def test_prepare_transitions_to_prepared(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.NEW)
        await flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED

    @pytest.mark.asyncio
    async def test_prepare_saves_payment(self, flow, mock_repo):
        payment = MockPayment(backend="mock")
        mock_repo._payments[payment.id] = payment
        await flow.prepare(payment)
        saved = await mock_repo.get_by_id(payment.id)
        assert saved.status == PaymentStatus.PREPARED


class TestHandleCallback:
    @pytest.mark.asyncio
    async def test_callback_applies_status(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)
        await flow.handle_callback(
            payment,
            data={"status": "confirm_payment"},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_callback_saves(self, flow, mock_repo):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)
        mock_repo._payments[payment.id] = payment
        await flow.handle_callback(
            payment,
            data={"status": "confirm_payment"},
            headers={},
        )
        saved = await mock_repo.get_by_id(payment.id)
        assert saved.status == PaymentStatus.PARTIAL


class TestFetchAndUpdateStatus:
    @pytest.mark.asyncio
    async def test_pull_updates_status(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)
        result = await flow.fetch_and_update_status(payment)
        assert result.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_pull_disallowed_callback(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)
        # Patch processor to return a disallowed callback
        with (
            patch.object(
                MockProcessor,
                "fetch_payment_status",
                new_callable=AsyncMock,
                return_value={"status": "flag_as_fraud"},
            ),
            pytest.raises(InvalidTransitionError),
        ):
            await flow.fetch_and_update_status(payment)


class TestCharge:
    @pytest.mark.asyncio
    async def test_charge_transitions_on_success(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PRE_AUTH)
        with patch.object(
            MockProcessor,
            "charge",
            new_callable=AsyncMock,
            return_value={
                "amount_charged": Decimal("100"),
                "success": True,
                "async_call": False,
            },
        ):
            result = await flow.charge(payment)
        assert result["success"] is True
        assert payment.status == PaymentStatus.IN_CHARGE


class TestReleaseLock:
    @pytest.mark.asyncio
    async def test_release_lock(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PRE_AUTH)
        with patch.object(
            MockProcessor,
            "release_lock",
            new_callable=AsyncMock,
            return_value=Decimal("100"),
        ):
            amount = await flow.release_lock(payment)
        assert amount == Decimal("100")
        assert payment.status == PaymentStatus.REFUNDED


class TestStartRefund:
    @pytest.mark.asyncio
    async def test_start_refund(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PAID)
        with patch.object(
            MockProcessor,
            "start_refund",
            new_callable=AsyncMock,
            return_value=Decimal("50"),
        ):
            amount = await flow.start_refund(payment)
        assert amount == Decimal("50")
        assert payment.status == PaymentStatus.REFUND_STARTED


class TestCancelRefund:
    @pytest.mark.asyncio
    async def test_cancel_refund_success(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.REFUND_STARTED,
        )
        with patch.object(
            MockProcessor,
            "cancel_refund",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await flow.cancel_refund(payment)
        assert result is True
        assert payment.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_cancel_refund_failure_no_transition(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.REFUND_STARTED,
        )
        with patch.object(
            MockProcessor,
            "cancel_refund",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await flow.cancel_refund(payment)
        assert result is False
        assert payment.status == PaymentStatus.REFUND_STARTED
