"""Tests for the built-in dummy payment backend."""

from decimal import Decimal
from decimal import localcontext
from typing import cast

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import BackendMethod
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.flow import PaymentFlow
from getpaid_core.processor import BaseProcessor
from getpaid_core.protocols import Payment as PaymentProtocol
from getpaid_core.registry import PluginRegistry
from tests.conftest import MockPayment
from tests.conftest import MockRepository


@pytest.fixture
def dummy_flow() -> PaymentFlow:
    """A PaymentFlow serving DummyProcessor alone."""
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(DummyProcessor)
    return PaymentFlow(repository=MockRepository(), registry=registry)


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


class TestDummyVerifyCallback:
    @pytest.mark.asyncio
    async def test_verify_callback_is_explicit_dev_only_noop(self) -> None:
        """DummyProcessor explicitly opts out of callback verification
        (dev-only backend) instead of inheriting the fail-closed default."""
        payment = MockPayment(backend="dummy")
        processor = DummyProcessor(cast("PaymentProtocol", payment))

        assert await processor.verify_callback({}, {}) is None


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


class TestDummyFallbackEventIds:
    """Callbacks that omit ``event_id`` must still advance cumulative
    progress, while an exact replay stays a harmless no-op."""

    @pytest.mark.asyncio
    async def test_cumulative_capture_without_explicit_ids(
        self, dummy_flow: PaymentFlow
    ) -> None:
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)

        for paid_amount in ("40.00", "100.00"):
            await dummy_flow.handle_callback(
                cast("PaymentProtocol", payment),
                {"event": "payment_confirmed", "paid_amount": paid_amount},
                {},
            )

        assert payment.amount_paid == Decimal("100.00")
        assert payment.status == PaymentStatus.PAID

    @pytest.mark.asyncio
    async def test_replayed_capture_is_recorded_once(
        self, dummy_flow: PaymentFlow
    ) -> None:
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)
        callback = {"event": "payment_confirmed", "paid_amount": "40.00"}

        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment), dict(callback), {}
        )
        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment), dict(callback), {}
        )

        assert payment.amount_paid == Decimal("40.00")
        assert payment.status == PaymentStatus.PARTIAL
        assert payment.provider_data["applied_event_ids"] == [
            "payment:pay-1:40"
        ]

    @pytest.mark.asyncio
    async def test_staged_refunds_without_explicit_ids(
        self, dummy_flow: PaymentFlow
    ) -> None:
        payment = MockPayment(
            backend="dummy",
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
        )

        for refunded_amount in ("40.00", "100.00"):
            await dummy_flow.handle_callback(
                cast("PaymentProtocol", payment),
                {
                    "event": "refund_confirmed",
                    "refunded_amount": refunded_amount,
                },
                {},
            )

        assert payment.amount_refunded == Decimal("100.00")
        assert payment.status == PaymentStatus.REFUNDED

    @pytest.mark.asyncio
    async def test_replayed_refund_is_recorded_once(
        self, dummy_flow: PaymentFlow
    ) -> None:
        payment = MockPayment(
            backend="dummy",
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
        )
        callback = {"event": "refund_confirmed", "refunded_amount": "40.00"}

        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment), dict(callback), {}
        )
        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment), dict(callback), {}
        )

        assert payment.amount_refunded == Decimal("40.00")
        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
        assert payment.provider_data["applied_event_ids"] == [
            "refund:pay-1:40"
        ]

    @pytest.mark.asyncio
    async def test_replayed_capture_ignores_amount_notation(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """Decimal-equal totals are the same event, however written."""
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)

        for paid_amount in ("40", "40.00", "40.000"):
            await dummy_flow.handle_callback(
                cast("PaymentProtocol", payment),
                {"event": "payment_confirmed", "paid_amount": paid_amount},
                {},
            )

        assert payment.provider_data["applied_event_ids"] == [
            "payment:pay-1:40"
        ]

    @pytest.mark.asyncio
    async def test_capture_and_refund_of_one_total_are_distinct_events(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """The fallback is scoped per family, not per amount alone."""
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)

        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment),
            {"event": "payment_confirmed", "paid_amount": "100.00"},
            {},
        )
        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment),
            {"event": "refund_confirmed", "refunded_amount": "100.00"},
            {},
        )

        assert payment.amount_paid == Decimal("100.00")
        assert payment.amount_refunded == Decimal("100.00")
        assert payment.status == PaymentStatus.REFUNDED

    @pytest.mark.asyncio
    async def test_explicit_event_id_overrides_the_fallback(
        self, dummy_flow: PaymentFlow
    ) -> None:
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)

        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment),
            {
                "event": "payment_confirmed",
                "paid_amount": "40.00",
                "event_id": "evt-1",
            },
            {},
        )

        assert payment.provider_data["applied_event_ids"] == ["evt-1"]

    @pytest.mark.asyncio
    async def test_blank_event_id_falls_back_instead_of_colliding(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """A null or empty ``event_id`` is an omitted one, not the
        literal string it would otherwise stringify into."""
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)

        for event_id in (None, ""):
            await dummy_flow.handle_callback(
                cast("PaymentProtocol", payment),
                {
                    "event": "payment_confirmed",
                    "paid_amount": "40.00",
                    "event_id": event_id,
                },
                {},
            )

        assert payment.provider_data["applied_event_ids"] == [
            "payment:pay-1:40"
        ]

    @pytest.mark.asyncio
    async def test_totals_differing_below_context_precision(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """Distinct totals stay distinct events even when the active
        Decimal context cannot represent the difference between them."""
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)

        with localcontext() as ctx:
            ctx.prec = 3
            for paid_amount in ("40.01", "40.02"):
                await dummy_flow.handle_callback(
                    cast("PaymentProtocol", payment),
                    {
                        "event": "payment_confirmed",
                        "paid_amount": paid_amount,
                    },
                    {},
                )

        assert payment.amount_paid == Decimal("40.02")
        assert payment.provider_data["applied_event_ids"] == [
            "payment:pay-1:40.01",
            "payment:pay-1:40.02",
        ]

    @pytest.mark.asyncio
    async def test_refund_totals_differing_below_context_precision(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """The refund family carries the same guarantee."""
        payment = MockPayment(
            backend="dummy",
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
        )

        with localcontext() as ctx:
            ctx.prec = 3
            for refunded_amount in ("40.01", "40.02"):
                await dummy_flow.handle_callback(
                    cast("PaymentProtocol", payment),
                    {
                        "event": "refund_confirmed",
                        "refunded_amount": refunded_amount,
                    },
                    {},
                )

        assert payment.amount_refunded == Decimal("40.02")
        assert payment.provider_data["applied_event_ids"] == [
            "refund:pay-1:40.01",
            "refund:pay-1:40.02",
        ]

    @pytest.mark.asyncio
    async def test_totals_differing_below_default_precision(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """Even the default context precision must not merge totals."""
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)
        totals = (
            "40.000000000000000000000000001",
            "40.000000000000000000000000002",
        )

        for paid_amount in totals:
            await dummy_flow.handle_callback(
                cast("PaymentProtocol", payment),
                {"event": "payment_confirmed", "paid_amount": paid_amount},
                {},
            )

        assert payment.amount_paid == Decimal(totals[1])
        assert payment.provider_data["applied_event_ids"] == [
            f"payment:pay-1:{totals[0]}",
            f"payment:pay-1:{totals[1]}",
        ]

    @pytest.mark.asyncio
    async def test_replay_identity_is_stable_across_contexts(
        self, dummy_flow: PaymentFlow
    ) -> None:
        """One total keeps one ID whatever context the callback arrives
        in, so a replay is still recognized as a replay."""
        payment = MockPayment(backend="dummy", status=PaymentStatus.PREPARED)
        callback = {"event": "payment_confirmed", "paid_amount": "40.00"}

        with localcontext() as ctx:
            ctx.prec = 3
            await dummy_flow.handle_callback(
                cast("PaymentProtocol", payment), dict(callback), {}
            )
        await dummy_flow.handle_callback(
            cast("PaymentProtocol", payment), dict(callback), {}
        )

        assert payment.amount_paid == Decimal("40.00")
        assert payment.provider_data["applied_event_ids"] == [
            "payment:pay-1:40"
        ]
