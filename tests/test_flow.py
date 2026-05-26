"""Tests for getpaid_core.flow.PaymentFlow."""

from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.flow import PaymentFlow
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from tests.conftest import MockOrder
from tests.conftest import MockPayment
from tests.conftest import MockProcessor


@pytest.fixture
def flow(mock_repo, mock_registry):
    return PaymentFlow(
        repository=mock_repo,
        config={"mock": {"sandbox": True}},
        registry=mock_registry,
    )


class TestCreatePayment:
    @pytest.mark.asyncio
    async def test_creates_payment(self, flow):
        order = MockOrder()
        payment = await flow.create_payment(order, "mock")
        assert payment.backend == "mock"
        assert payment.amount_required == Decimal("100.00")
        assert payment.currency == "PLN"
        assert payment.provider_data == {}

    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self, flow):
        order = MockOrder()
        with pytest.raises(KeyError):
            await flow.create_payment(order, "nonexistent")


class TestPrepare:
    @pytest.mark.asyncio
    async def test_prepare_stores_external_id_and_provider_data(self, flow):
        payment = MockPayment(backend="mock")
        result = await flow.prepare(payment)

        assert result.redirect_url == "https://mock.example.com/pay"
        assert payment.external_id == "ext-pay-1"
        assert payment.provider_data["customer_ip"] == "127.0.0.1"
        assert payment.status == PaymentStatus.PREPARED

    @pytest.mark.asyncio
    async def test_prepare_uses_validator_mutated_kwargs(
        self, mock_repo, mock_registry
    ):
        def add_customer_ip(context):
            context["kwargs"]["customer_ip"] = "10.0.0.8"
            return context

        flow = PaymentFlow(
            repository=mock_repo,
            config={"mock": {"sandbox": True}},
            validators=[add_customer_ip],
            registry=mock_registry,
        )
        payment = MockPayment(backend="mock")

        await flow.prepare(payment)

        assert payment.provider_data["customer_ip"] == "10.0.0.8"


class TestHandleCallback:
    @pytest.mark.asyncio
    async def test_callback_applies_semantic_update(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)

        await flow.handle_callback(
            payment,
            data={"event": "payment_confirmed", "paid_amount": "100.00"},
            headers={},
        )

        assert payment.status == PaymentStatus.PAID
        assert payment.amount_paid == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_verify_failure_does_not_save(self, flow, mock_repo):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)
        mock_repo._payments[payment.id] = payment

        with (
            patch.object(
                MockProcessor,
                "verify_callback",
                new_callable=AsyncMock,
                side_effect=InvalidCallbackError("bad signature"),
            ),
            pytest.raises(InvalidCallbackError),
        ):
            await flow.handle_callback(payment, data={}, headers={})

        assert mock_repo.save_calls == 0


class TestFetchAndUpdateStatus:
    @pytest.mark.asyncio
    async def test_pull_updates_payment(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)

        result = await flow.fetch_and_update_status(payment)

        assert result.status == PaymentStatus.PAID
        assert result.amount_paid == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_duplicate_provider_event_is_idempotent(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PREPARED)

        with patch.object(
            MockProcessor,
            "fetch_payment_status",
            new_callable=AsyncMock,
            return_value=PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100.00"),
                provider_event_id="pull-dup-1",
            ),
        ):
            await flow.fetch_and_update_status(payment)
            await flow.fetch_and_update_status(payment)

        assert payment.amount_paid == Decimal("100.00")
        assert payment.provider_data["applied_event_ids"] == ["pull-dup-1"]


class TestCharge:
    @pytest.mark.asyncio
    async def test_synchronous_charge_marks_payment_as_paid(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.PRE_AUTH,
            amount_locked=Decimal("100.00"),
        )

        result = await flow.charge(payment)

        assert isinstance(result, ChargeResult)
        assert payment.status == PaymentStatus.PAID
        assert payment.amount_paid == Decimal("100.00")
        assert payment.amount_locked == Decimal("0.00")

    @pytest.mark.asyncio
    async def test_async_charge_marks_payment_in_charge(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.PRE_AUTH,
            amount_locked=Decimal("50.00"),
        )

        with patch.object(
            MockProcessor,
            "charge",
            new_callable=AsyncMock,
            return_value=ChargeResult(
                amount_charged=Decimal("50.00"),
                success=True,
                async_call=True,
            ),
        ):
            await flow.charge(payment)

        assert payment.status == PaymentStatus.IN_CHARGE
        assert payment.amount_paid == Decimal("0")


class TestRefunds:
    @pytest.mark.asyncio
    async def test_start_refund_stores_provider_data(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
        )

        result = await flow.start_refund(payment)

        assert isinstance(result, RefundResult)
        assert result.amount == Decimal("100.00")
        assert payment.status == PaymentStatus.REFUND_STARTED
        assert payment.provider_data["refund_id"] == "refund-1"

    @pytest.mark.asyncio
    async def test_cancel_refund_restores_paid_status(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
            amount_required=Decimal("100.00"),
        )

        result = await flow.cancel_refund(payment)

        assert result is True
        assert payment.status == PaymentStatus.PAID


class TestProcessorAccess:
    def test_get_processor_uses_injected_registry(self, flow):
        payment = MockPayment(backend="mock")
        processor = flow.get_processor(payment)

        assert isinstance(processor, MockProcessor)
        assert processor.config == {"sandbox": True}


class TestPreconditions:
    """Precondition validation prevents unnecessary API calls."""

    @pytest.mark.asyncio
    async def test_charge_requires_pre_auth_or_in_charge(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.NEW)

        with pytest.raises(InvalidTransitionError, match="Cannot charge"):
            await flow.charge(payment)

    @pytest.mark.asyncio
    async def test_charge_invalidates_before_api_call(self, flow):
        """Charge rejection must happen before the processor is called."""
        payment = MockPayment(backend="mock", status=PaymentStatus.NEW)

        with patch.object(
            MockProcessor,
            "charge",
            new_callable=AsyncMock,
        ) as mock_charge:
            with pytest.raises(InvalidTransitionError):
                await flow.charge(payment)
            mock_charge.assert_not_called()

    @pytest.mark.asyncio
    async def test_release_lock_requires_pre_auth(self, flow):
        payment = MockPayment(backend="mock", status=PaymentStatus.PAID)

        with pytest.raises(InvalidTransitionError, match="Cannot release lock"):
            await flow.release_lock(payment)

    @pytest.mark.asyncio
    async def test_start_refund_requires_paid_or_partial(self, flow):
        payment = MockPayment(
            backend="mock",
            status=PaymentStatus.NEW,
        )

        with pytest.raises(InvalidTransitionError, match="Cannot start refund"):
            await flow.start_refund(payment)
