"""Partial capture, release eligibility and status projection (ADR 0001).

Sections 4 and 5 of the ADR make captured funds, refunded funds and the
remaining authorization orthogonal facts, and derive the public status
from them rather than from the previous status alone. These tests pin the
two worked examples the contract states, the eligibility rules that let
them run, and the projection precedence that reports them truthfully.
"""

from decimal import Decimal

import pytest

from getpaid_core._amounts import validate_payment_amounts
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.rules import plan_reservation
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.flow import PaymentFlow
from getpaid_core.fsm import apply_payment_update
from getpaid_core.fsm import project_payment_status
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from tests.conftest import MockPayment
from tests.conftest import MockProcessor
from tests.conftest import MockRepository


class RecordingProcessor(MockProcessor):
    """A processor that records exactly which commands it received.

    The contract's examples are as much about *what reached the provider*
    as about the totals afterwards, so the calls are asserted directly
    rather than inferred from the resulting amounts.
    """

    slug = "recording"
    calls: list[tuple[str, Decimal | None]] = []

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResult:
        type(self).calls.append(("charge", amount))
        return await super().charge(amount=amount, **kwargs)

    async def release_lock(self, **kwargs) -> Decimal:
        type(self).calls.append(("release_lock", self.payment.amount_locked))
        return await super().release_lock(**kwargs)

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> RefundResult:
        type(self).calls.append(("start_refund", amount))
        return await super().start_refund(amount=amount, **kwargs)


@pytest.fixture
def recorder() -> type[RecordingProcessor]:
    RecordingProcessor.reset()
    return RecordingProcessor


@pytest.fixture
def flow(recorder: type[RecordingProcessor]) -> PaymentFlow:
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(recorder)
    return PaymentFlow(MockRepository(), registry=registry)


def authorized_payment(
    required: str = "100.00", locked: str = "100.00"
) -> MockPayment:
    """A payment holding a fresh authorization for the whole amount."""
    payment = MockPayment(
        backend=RecordingProcessor.slug,
        amount_required=Decimal(required),
        status=PaymentStatus.PREPARED,
    )
    apply_payment_update(
        payment,
        PaymentUpdate(
            payment_event=PaymentEvent.LOCKED,
            locked_amount=Decimal(locked),
        ),
    )
    return payment


class TestSubsequentCapture:
    """The contract's two worked examples for required=100."""

    @pytest.mark.asyncio
    async def test_partial_capture_then_remaining_capture_pays_in_full(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        """lock(100) -> capture(30) -> capture(70) ends fully paid.

        The remaining authorization stays usable: the second capture is a
        supported command, not a transition the previous status forbids.
        """
        payment = authorized_payment()

        await flow.charge(payment, amount=Decimal("30.00"))
        assert payment.status == PaymentStatus.PARTIAL
        assert payment.amount_locked == Decimal("70.00")

        await flow.charge(payment, amount=Decimal("70.00"))

        assert recorder.calls == [
            ("charge", Decimal("30.00")),
            ("charge", Decimal("70.00")),
        ]
        assert payment.amount_paid == Decimal("100.00")
        assert payment.amount_refunded == Decimal("0")
        assert payment.amount_locked == Decimal("0.00")
        assert payment.status == PaymentStatus.PAID
        validate_payment_amounts(payment)

    @pytest.mark.asyncio
    async def test_partial_capture_then_release_leaves_partially_paid(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        """lock(100) -> capture(30) -> release(70) ends partially paid.

        Releasing an uncaptured hold returns nothing to the buyer, so the
        captured 30 must not be reported as refunded.
        """
        payment = authorized_payment()

        await flow.charge(payment, amount=Decimal("30.00"))
        released = await flow.release_lock(payment)

        assert released == Decimal("70.00")
        assert recorder.calls == [
            ("charge", Decimal("30.00")),
            ("release_lock", Decimal("70.00")),
        ]
        assert payment.amount_paid == Decimal("30.00")
        assert payment.amount_refunded == Decimal("0")
        assert payment.amount_locked == Decimal("0.00")
        assert payment.status == PaymentStatus.PARTIAL
        validate_payment_amounts(payment)

    @pytest.mark.asyncio
    async def test_uncaptured_release_cancels(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        """A full release with nothing captured is a cancellation."""
        payment = authorized_payment()

        released = await flow.release_lock(payment)

        assert released == Decimal("100.00")
        assert recorder.calls == [("release_lock", Decimal("100.00"))]
        assert payment.amount_paid == Decimal("0")
        assert payment.amount_refunded == Decimal("0")
        assert payment.amount_locked == Decimal("0.00")
        assert payment.status == PaymentStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_capture_beyond_remaining_authorization_is_refused(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        """Eligibility is bounded by the hold, not merely non-empty."""
        payment = authorized_payment()
        await flow.charge(payment, amount=Decimal("30.00"))
        recorder.reset()

        with pytest.raises(InvalidTransitionError, match="Charge amount"):
            await flow.charge(payment, amount=Decimal("80.00"))

        assert recorder.calls == []
        assert payment.amount_paid == Decimal("30.00")

    @pytest.mark.asyncio
    async def test_capture_without_authorization_is_refused(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        """Nothing is authorized, so no capture reaches the provider."""
        payment = MockPayment(
            backend=RecordingProcessor.slug, status=PaymentStatus.PREPARED
        )

        with pytest.raises(
            InvalidTransitionError, match="no remaining authorization"
        ):
            await flow.charge(payment)

        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_release_without_a_hold_is_refused(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        """A fully captured payment has no authorization left to release."""
        payment = authorized_payment()
        await flow.charge(payment, amount=Decimal("100.00"))
        recorder.reset()

        with pytest.raises(
            InvalidTransitionError, match="Cannot release the authorization"
        ):
            await flow.release_lock(payment)

        assert recorder.calls == []
        assert payment.status == PaymentStatus.PAID


class TestRefundDoesNotReopenCaptureCapacity:
    """Replacement collection uses a new payment, not the refunded one."""

    @pytest.mark.asyncio
    async def test_capture_after_a_refund_is_refused(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        payment = authorized_payment()
        await flow.charge(payment, amount=Decimal("30.00"))
        await flow.start_refund(payment, amount=Decimal("30.00"))
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=Decimal("30.00"),
            ),
        )
        recorder.reset()

        with pytest.raises(InvalidTransitionError, match="refunded"):
            await flow.charge(payment, amount=Decimal("70.00"))

        assert recorder.calls == []
        assert payment.amount_paid == Decimal("30.00")
        assert payment.amount_refunded == Decimal("30.00")

    @pytest.mark.asyncio
    async def test_capture_while_a_refund_is_unresolved_is_refused(
        self, flow: PaymentFlow, recorder: type[RecordingProcessor]
    ) -> None:
        payment = authorized_payment()
        await flow.charge(payment, amount=Decimal("30.00"))
        await flow.start_refund(payment, amount=Decimal("30.00"))
        recorder.reset()

        with pytest.raises(InvalidTransitionError, match="refund"):
            await flow.charge(payment, amount=Decimal("70.00"))

        assert recorder.calls == []

    def test_durable_reservation_refuses_capture_after_a_refund(self) -> None:
        facts = PaymentFacts(
            payment_id="pay-1",
            amount_required=Decimal("100.00"),
            captured_funds=Decimal("30.00"),
            refunded_funds=Decimal("30.00"),
            remaining_authorization=Decimal("70.00"),
            status=PaymentStatus.REFUNDED,
        )

        with pytest.raises(InvalidTransitionError, match="refunded"):
            plan_reservation(
                facts,
                (),
                OperationIntent(
                    operation_id="op-1",
                    operation_type=OperationType.CHARGE,
                    amount=Decimal("70.00"),
                ),
            )


class TestAuthorizationRelease:
    """Releasing removes the hold and moves no settled funds."""

    def test_release_after_a_refund_keeps_the_totals(self) -> None:
        """A release changes neither captured nor refunded funds."""
        payment = MockPayment(
            status=PaymentStatus.PARTIALLY_REFUNDED,
            amount_paid=Decimal("60.00"),
            amount_refunded=Decimal("20.00"),
            amount_locked=Decimal("40.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
        )

        assert payment.amount_paid == Decimal("60.00")
        assert payment.amount_refunded == Decimal("20.00")
        assert payment.amount_locked == Decimal("0.00")
        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED

    def test_durable_reservation_allows_release_after_a_refund(self) -> None:
        """A refund blocks further capture, never the release."""
        facts = PaymentFacts(
            payment_id="pay-1",
            amount_required=Decimal("100.00"),
            captured_funds=Decimal("30.00"),
            refunded_funds=Decimal("30.00"),
            remaining_authorization=Decimal("70.00"),
            status=PaymentStatus.REFUNDED,
        )

        plan = plan_reservation(
            facts,
            (),
            OperationIntent(
                operation_id="op-1",
                operation_type=OperationType.RELEASE_LOCK,
            ),
        )

        assert plan.operation.resolved_amount == Decimal("70.00")


class TestCaptureEvidenceIsNotACaptureCommand:
    """Recording what happened is narrower than authorizing it.

    ADR 0001, section 5: an equal or lower cumulative capture observed
    alongside refund progress must be absorbed without regressing either
    total. The command rule that refuses capture after a refund must not
    leak onto the observation path -- the durable conformance suite races
    a stale capture against a refund and expects both totals to survive.
    """

    def test_stale_capture_during_a_partial_refund_is_absorbed(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.PARTIALLY_REFUNDED,
            amount_paid=Decimal("100.00"),
            amount_refunded=Decimal("40.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("40.00"),
            ),
        )

        assert payment.amount_paid == Decimal("100.00")
        assert payment.amount_refunded == Decimal("40.00")
        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED

    def test_capture_evidence_after_a_partial_refund_is_recorded(self) -> None:
        """Genuinely new money is a fact, not a command to authorize."""
        payment = MockPayment(
            status=PaymentStatus.PARTIALLY_REFUNDED,
            amount_paid=Decimal("60.00"),
            amount_refunded=Decimal("20.00"),
            amount_locked=Decimal("40.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("80.00"),
            ),
        )

        assert payment.amount_paid == Decimal("80.00")
        assert payment.amount_refunded == Decimal("20.00")
        assert payment.amount_locked == Decimal("20.00")
        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED

    def test_capture_evidence_on_a_fully_returned_payment_still_raises(
        self,
    ) -> None:
        """Unchanged from the released contract.

        ADR 0001, section 5 wants this recorded with a reconciliation
        requirement instead of refused; that needs reconciliation
        machinery this slice does not own.
        """
        payment = MockPayment(
            status=PaymentStatus.REFUNDED,
            amount_paid=Decimal("100.00"),
            amount_refunded=Decimal("100.00"),
        )

        with pytest.raises(
            InvalidTransitionError, match=r"captured funds were all"
        ):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.PAYMENT_CAPTURED,
                    paid_amount=Decimal("100.00"),
                ),
            )

    def test_capture_evidence_while_a_refund_is_unresolved_raises(
        self,
    ) -> None:
        """Unchanged from the released contract."""
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
        )

        with pytest.raises(InvalidTransitionError, match="refund is unresolved"):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.PAYMENT_CAPTURED,
                    paid_amount=Decimal("100.00"),
                ),
            )


class TestStatusProjection:
    """The precedence of ADR 0001, section 4."""

    def test_unresolved_refund_wins_over_the_amounts(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
        )

        assert (
            project_payment_status(payment, refund_in_progress=True)
            == PaymentStatus.REFUND_STARTED
        )

    def test_full_refund_reports_refunded(self) -> None:
        payment = MockPayment(
            amount_paid=Decimal("40.00"), amount_refunded=Decimal("40.00")
        )

        assert project_payment_status(payment) == PaymentStatus.REFUNDED

    def test_partial_refund_is_distinct_from_partial_payment(self) -> None:
        """Partially refunded and partially paid are different facts."""
        partially_refunded = MockPayment(
            amount_paid=Decimal("100.00"), amount_refunded=Decimal("40.00")
        )
        partially_paid = MockPayment(amount_paid=Decimal("40.00"))

        assert (
            project_payment_status(partially_refunded)
            == PaymentStatus.PARTIALLY_REFUNDED
        )
        assert project_payment_status(partially_paid) == PaymentStatus.PARTIAL

    def test_full_capture_reports_paid(self) -> None:
        payment = MockPayment(amount_paid=Decimal("100.00"))

        assert project_payment_status(payment) == PaymentStatus.PAID

    def test_hold_without_capture_reports_authorized(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.PREPARED, amount_locked=Decimal("100.00")
        )

        assert project_payment_status(payment) == PaymentStatus.PRE_AUTH

    def test_zero_totals_alone_are_not_cancellation(self) -> None:
        """Only a confirmed release cancels; an empty payment does not."""
        payment = MockPayment(status=PaymentStatus.PREPARED)

        assert project_payment_status(payment) == PaymentStatus.PREPARED
        assert (
            project_payment_status(payment, authorization_released=True)
            == PaymentStatus.CANCELLED
        )

    def test_nonfinancial_statuses_are_preserved(self) -> None:
        """Preparation and failure survive where no settlement rule applies."""
        for status in (
            PaymentStatus.NEW,
            PaymentStatus.PREPARED,
            PaymentStatus.FAILED,
            PaymentStatus.IN_CHARGE,
        ):
            payment = MockPayment(status=status)

            assert project_payment_status(payment) == status

    def test_partial_refund_of_a_partial_capture_reports_partially_refunded(
        self,
    ) -> None:
        """Refund progress outranks the capture that is still outstanding."""
        payment = MockPayment(
            status=PaymentStatus.PARTIAL,
            amount_paid=Decimal("60.00"),
            amount_refunded=Decimal("20.00"),
            amount_locked=Decimal("40.00"),
        )

        assert (
            project_payment_status(payment)
            == PaymentStatus.PARTIALLY_REFUNDED
        )

    def test_hold_stays_visible_alongside_the_projected_status(self) -> None:
        """The status is a projection, not the whole financial state."""
        payment = MockPayment(
            status=PaymentStatus.PARTIAL,
            amount_paid=Decimal("30.00"),
            amount_locked=Decimal("70.00"),
        )

        assert project_payment_status(payment) == PaymentStatus.PARTIAL
        assert payment.amount_locked == Decimal("70.00")


class TestRefundLifecycleOnFacts:
    """Refund eligibility follows captured minus refunded funds."""

    def test_further_refund_allowed_while_funds_remain(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.PARTIALLY_REFUNDED,
            amount_paid=Decimal("100.00"),
            amount_refunded=Decimal("40.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.REFUND_REQUESTED),
        )

        assert payment.status == PaymentStatus.REFUND_STARTED

    def test_refund_refused_once_every_captured_fund_is_returned(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.REFUNDED,
            amount_paid=Decimal("100.00"),
            amount_refunded=Decimal("100.00"),
        )

        with pytest.raises(InvalidTransitionError, match="start refund"):
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.REFUND_REQUESTED),
            )

    def test_confirming_a_partial_refund_projects_partially_refunded(
        self,
    ) -> None:
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=Decimal("40.00"),
            ),
        )

        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
        assert payment.amount_refunded == Decimal("40.00")

    def test_cancelling_a_refund_projects_the_remaining_facts(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
            amount_refunded=Decimal("40.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED),
        )

        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED
