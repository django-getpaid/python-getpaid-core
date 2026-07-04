"""Tests for the semantic payment state engine."""

from decimal import Decimal

import pytest

from getpaid_core.enums import FraudEvent
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.fsm import apply_payment_update
from getpaid_core.types import PaymentUpdate
from tests.conftest import MockPayment


class TestApplyPaymentUpdate:
    def test_prepare_from_new(self) -> None:
        payment = MockPayment(status=PaymentStatus.NEW)

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.PREPARED),
        )

        assert payment.status == PaymentStatus.PREPARED

    def test_confirm_payment_sets_paid_for_full_amount(self) -> None:
        payment = MockPayment(status=PaymentStatus.PREPARED)

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100.00"),
            ),
        )

        assert payment.status == PaymentStatus.PAID
        assert payment.amount_paid == Decimal("100.00")

    def test_confirm_payment_keeps_partial_for_incomplete_amount(self) -> None:
        payment = MockPayment(status=PaymentStatus.PREPARED)

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("40.00"),
            ),
        )

        assert payment.status == PaymentStatus.PARTIAL
        assert payment.amount_paid == Decimal("40.00")

    def test_duplicate_event_id_is_ignored(self) -> None:
        payment = MockPayment(status=PaymentStatus.PREPARED)
        update = PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("100.00"),
            provider_event_id="evt-1",
        )

        apply_payment_update(payment, update)
        apply_payment_update(payment, update)

        assert payment.amount_paid == Decimal("100.00")
        assert payment.provider_data["applied_event_ids"] == ["evt-1"]

    def test_refund_confirmation_marks_payment_refunded(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=Decimal("100.00"),
            ),
        )

        assert payment.status == PaymentStatus.REFUNDED
        assert payment.amount_refunded == Decimal("100.00")

    def test_refund_cancelled_restores_paid_status(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_paid=Decimal("100.00"),
            amount_required=Decimal("100.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED),
        )

        assert payment.status == PaymentStatus.PAID

    def test_invalid_transition_raises_domain_error(self) -> None:
        payment = MockPayment(status=PaymentStatus.PAID)

        with pytest.raises(InvalidTransitionError):
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.FAILED),
            )

    def test_invalid_transition_keeps_payment_metadata_atomic(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
            external_id="ext-original",
            fraud_message="unchanged",
            provider_data={"existing": "value"},
        )

        with pytest.raises(InvalidTransitionError):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.FAILED,
                    external_id="ext-new",
                    fraud_message="should not stick",
                    provider_event_id="evt-invalid",
                    provider_data={"new": "data"},
                ),
            )

        assert payment.status == PaymentStatus.PAID
        assert payment.amount_paid == Decimal("100.00")
        assert payment.external_id == "ext-original"
        assert payment.fraud_message == "unchanged"
        assert payment.provider_data == {"existing": "value"}

    def test_invalid_fraud_event_rolls_back_payment_changes(self) -> None:
        payment = MockPayment(
            status=PaymentStatus.PREPARED,
            fraud_status=FraudStatus.ACCEPTED,
            provider_data={"existing": "value"},
        )

        with pytest.raises(InvalidTransitionError):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.PAYMENT_CAPTURED,
                    paid_amount=Decimal("100.00"),
                    fraud_event=FraudEvent.REVIEW,
                    external_id="ext-new",
                    provider_event_id="evt-mixed-invalid",
                    provider_data={"new": "data"},
                ),
            )

        assert payment.status == PaymentStatus.PREPARED
        assert payment.amount_paid == Decimal("0")
        assert payment.external_id is None
        assert payment.fraud_status == FraudStatus.ACCEPTED
        assert payment.provider_data == {"existing": "value"}

    def test_prepared_in_wrong_status_raises(self) -> None:
        """PREPARED outside NEW must raise like every other event, not
        silently return."""
        payment = MockPayment(
            status=PaymentStatus.PAID, amount_paid=Decimal("100.00")
        )

        with pytest.raises(InvalidTransitionError, match="Cannot prepare"):
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.PREPARED),
            )

        assert payment.status == PaymentStatus.PAID

    def test_locked_in_wrong_status_raises(self) -> None:
        """LOCKED outside NEW/PREPARED/PRE_AUTH must raise, not silently
        return."""
        payment = MockPayment(
            status=PaymentStatus.PAID, amount_paid=Decimal("100.00")
        )

        with pytest.raises(InvalidTransitionError, match="Cannot lock"):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.LOCKED,
                    locked_amount=Decimal("50.00"),
                ),
            )

        assert payment.status == PaymentStatus.PAID

    def test_fraud_event_updates_message(self) -> None:
        payment = MockPayment(fraud_status=FraudStatus.UNKNOWN)

        apply_payment_update(
            payment,
            PaymentUpdate(
                fraud_event=FraudEvent.REVIEW,
                fraud_message="Manual review required",
            ),
        )

        assert payment.fraud_status == FraudStatus.CHECK
        assert payment.fraud_message == "Manual review required"


class TestProviderEventDedupe:
    """Duplicate provider events are detected via an O(1) set view while
    provider_data keeps the serialized list representation unchanged."""

    def test_stored_representation_stays_a_list(self) -> None:
        payment = MockPayment(status=PaymentStatus.PREPARED)

        for i in range(3):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    provider_event_id=f"evt-{i}",
                    provider_data={"seen": i},
                ),
            )

        applied = payment.provider_data["applied_event_ids"]
        assert isinstance(applied, list)
        assert applied == ["evt-0", "evt-1", "evt-2"]

    def test_set_view_tracks_the_list(self) -> None:
        payment = MockPayment(status=PaymentStatus.PREPARED)

        apply_payment_update(payment, PaymentUpdate(provider_event_id="evt-a"))
        apply_payment_update(payment, PaymentUpdate(provider_event_id="evt-b"))

        cached_list, cached_set = payment._getpaid_applied_event_ids_cache
        assert cached_list is payment.provider_data["applied_event_ids"]
        assert cached_set == {"evt-a", "evt-b"}

    def test_dedupe_survives_rollback(self) -> None:
        """A rolled-back update must not leave its event id in the set
        view: the same id must still be applicable afterwards, and then
        deduped on a genuine replay."""
        payment = MockPayment(status=PaymentStatus.PREPARED)

        apply_payment_update(payment, PaymentUpdate(provider_event_id="evt-a"))

        with pytest.raises(InvalidTransitionError):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.PAYMENT_CAPTURED,
                    paid_amount=Decimal("999.00"),
                    provider_event_id="evt-b",
                ),
            )
        assert payment.provider_data["applied_event_ids"] == ["evt-a"]

        # evt-b was rolled back, so it must apply cleanly now...
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100.00"),
                provider_event_id="evt-b",
            ),
        )
        assert payment.amount_paid == Decimal("100.00")

        # ...and be deduped on replay.
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100.00"),
                provider_event_id="evt-b",
            ),
        )
        assert payment.provider_data["applied_event_ids"] == [
            "evt-a",
            "evt-b",
        ]

    def test_externally_replaced_list_is_respected(self) -> None:
        """If the stored list is replaced (e.g. payment reloaded from the
        database), the set view is rebuilt from it."""
        payment = MockPayment(status=PaymentStatus.PREPARED)
        apply_payment_update(payment, PaymentUpdate(provider_event_id="evt-a"))

        payment.provider_data["applied_event_ids"] = ["evt-x"]

        assert (
            apply_payment_update(
                payment, PaymentUpdate(provider_event_id="evt-x")
            )
            is payment
        )
        # evt-x was already applied in the replaced list -> deduped.
        assert payment.provider_data["applied_event_ids"] == ["evt-x"]


class TestPaidAmountValidation:
    """amount_paid must never exceed amount_required."""

    def test_paid_amount_exceeding_required_raises(self) -> None:
        """PAYMENT_CAPTURED with paid_amount > amount_required must raise."""
        payment = MockPayment(
            status=PaymentStatus.PREPARED,
            amount_required=Decimal("100.00"),
        )

        with pytest.raises(
            InvalidTransitionError,
            match="exceeds amount_required",
        ):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.PAYMENT_CAPTURED,
                    paid_amount=Decimal("150.00"),
                ),
            )

        assert payment.status == PaymentStatus.PREPARED
        assert payment.amount_paid == Decimal("0")

    def test_paid_amount_equal_to_required_succeeds(self) -> None:
        """PAYMENT_CAPTURED with paid_amount == amount_required is valid."""
        payment = MockPayment(
            status=PaymentStatus.PREPARED,
            amount_required=Decimal("100.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100.00"),
            ),
        )

        assert payment.status == PaymentStatus.PAID
        assert payment.amount_paid == Decimal("100.00")

    def test_paid_amount_accumulates_up_to_required(self) -> None:
        """Cumulative captures sum up to amount_required."""
        payment = MockPayment(
            status=PaymentStatus.PREPARED,
            amount_required=Decimal("100.00"),
        )

        # First capture: cumulative paid = 60
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("60.00"),
            ),
        )
        assert payment.amount_paid == Decimal("60.00")
        assert payment.status == PaymentStatus.PARTIAL

        # Second capture: cumulative paid = 100 (60 + 40)
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100.00"),
            ),
        )
        assert payment.amount_paid == Decimal("100.00")
        assert payment.status == PaymentStatus.PAID


class TestRefundedAmountValidation:
    """amount_refunded must never exceed amount_paid."""

    def test_refunded_amount_exceeding_paid_raises(self) -> None:
        """REFUND_CONFIRMED with refunded_amount > amount_paid must raise."""
        payment = MockPayment(
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
        )

        with pytest.raises(
            InvalidTransitionError,
            match="exceeds amount_paid",
        ):
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.REFUND_REQUESTED,
                ),
            )
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.REFUND_CONFIRMED,
                    refunded_amount=Decimal("150.00"),
                ),
            )

        assert payment.status == PaymentStatus.REFUND_STARTED
        assert payment.amount_refunded == Decimal("0")

    def test_refunded_amount_equal_to_paid_succeeds(self) -> None:
        """REFUND_CONFIRMED with refunded_amount == amount_paid is valid."""
        payment = MockPayment(
            status=PaymentStatus.PAID,
            amount_paid=Decimal("100.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.REFUND_REQUESTED),
        )
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=Decimal("100.00"),
            ),
        )

        assert payment.status == PaymentStatus.REFUNDED
        assert payment.amount_refunded == Decimal("100.00")


class TestLockReleased:
    """LOCK_RELEASED event transitions: only PRE_AUTH is valid."""

    def test_lock_released_with_zero_paid_cancels_payment(self) -> None:
        """LOCK_RELEASED on PRE_AUTH with nothing paid sets
        amount_locked=0 and status=CANCELLED -- no money moved, so the
        payment was cancelled, not refunded."""
        payment = MockPayment(
            status=PaymentStatus.PRE_AUTH,
            amount_locked=Decimal("100.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
        )

        assert payment.status == PaymentStatus.CANCELLED
        assert payment.amount_locked == Decimal("0.00")

    def test_lock_released_with_partial_paid_marks_refunded(self) -> None:
        """LOCK_RELEASED when some amount was already captured keeps the
        historical REFUNDED status."""
        payment = MockPayment(
            status=PaymentStatus.PRE_AUTH,
            amount_paid=Decimal("30.00"),
            amount_locked=Decimal("70.00"),
        )

        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
        )

        assert payment.status == PaymentStatus.REFUNDED
        assert payment.amount_locked == Decimal("0.00")

    def test_lock_released_from_refunded_raises(self) -> None:
        """LOCK_RELEASED on REFUNDED must raise — a refunded payment
        has no lock to release."""
        payment = MockPayment(
            status=PaymentStatus.REFUNDED,
            amount_locked=Decimal("0.00"),
        )

        with pytest.raises(
            InvalidTransitionError,
            match="Cannot release lock",
        ):
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
            )

    def test_lock_released_from_paid_raises(self) -> None:
        """LOCK_RELEASED on PAID must raise — a paid payment has no lock."""
        payment = MockPayment(
            status=PaymentStatus.PAID,
            amount_locked=Decimal("0.00"),
        )

        with pytest.raises(
            InvalidTransitionError,
            match="Cannot release lock",
        ):
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
            )

    def test_lock_released_from_partial_raises(self) -> None:
        """LOCK_RELEASED on PARTIAL must raise."""
        payment = MockPayment(
            status=PaymentStatus.PARTIAL,
            amount_locked=Decimal("0.00"),
        )

        with pytest.raises(
            InvalidTransitionError,
            match="Cannot release lock",
        ):
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
            )
