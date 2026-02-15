"""Tests for getpaid_core.fsm -- payment state machine."""

import pytest

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.fsm import ALLOWED_CALLBACKS
from getpaid_core.fsm import create_fraud_machine
from getpaid_core.fsm import create_payment_machine


class MockPayment:
    """Minimal object for FSM attachment."""

    def __init__(
        self,
        status=PaymentStatus.NEW,
        fraud_status=FraudStatus.UNKNOWN,
    ):
        self.status = status
        self.fraud_status = fraud_status
        self.amount_required = 100
        self.amount_paid = 0
        self.amount_locked = 0
        self.amount_refunded = 0
        self.fraud_message = ""

    def is_fully_paid(self):
        return self.amount_paid >= self.amount_required

    def is_fully_refunded(self):
        return self.amount_refunded >= self.amount_paid


# === Payment FSM: valid transitions ===


class TestPaymentPrepare:
    def test_new_to_prepared(self):
        p = MockPayment(status=PaymentStatus.NEW)
        create_payment_machine(p)
        p.confirm_prepared()
        assert p.status == PaymentStatus.PREPARED


class TestPaymentLock:
    def test_new_to_pre_auth(self):
        p = MockPayment(status=PaymentStatus.NEW)
        create_payment_machine(p)
        p.confirm_lock()
        assert p.status == PaymentStatus.PRE_AUTH

    def test_prepared_to_pre_auth(self):
        p = MockPayment(status=PaymentStatus.PREPARED)
        create_payment_machine(p)
        p.confirm_lock()
        assert p.status == PaymentStatus.PRE_AUTH


class TestPaymentCharge:
    def test_pre_auth_to_in_charge(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.confirm_charge_sent()
        assert p.status == PaymentStatus.IN_CHARGE


class TestPaymentConfirmPayment:
    def test_pre_auth_to_partial(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL

    def test_prepared_to_partial(self):
        p = MockPayment(status=PaymentStatus.PREPARED)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL

    def test_in_charge_to_partial(self):
        p = MockPayment(status=PaymentStatus.IN_CHARGE)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL

    def test_partial_stays_partial(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL


class TestPaymentMarkAsPaid:
    def test_partial_to_paid_when_fully_paid(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 100
        create_payment_machine(p)
        p.mark_as_paid()
        assert p.status == PaymentStatus.PAID

    def test_partial_to_paid_blocked_when_not_fully_paid(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 50
        create_payment_machine(p)
        # transitions raises MachineError when condition fails
        from transitions.core import MachineError

        with pytest.raises(MachineError):
            p.mark_as_paid()
        assert p.status == PaymentStatus.PARTIAL


class TestPaymentReleaseLock:
    def test_pre_auth_to_refunded(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.release_lock()
        assert p.status == PaymentStatus.REFUNDED


class TestPaymentRefundFlow:
    def test_paid_to_refund_started(self):
        p = MockPayment(status=PaymentStatus.PAID)
        create_payment_machine(p)
        p.start_refund()
        assert p.status == PaymentStatus.REFUND_STARTED

    def test_partial_to_refund_started(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        create_payment_machine(p)
        p.start_refund()
        assert p.status == PaymentStatus.REFUND_STARTED

    def test_cancel_refund_to_partial(self):
        p = MockPayment(status=PaymentStatus.REFUND_STARTED)
        create_payment_machine(p)
        p.cancel_refund()
        assert p.status == PaymentStatus.PARTIAL

    def test_confirm_refund_to_partial(self):
        p = MockPayment(status=PaymentStatus.REFUND_STARTED)
        create_payment_machine(p)
        p.confirm_refund()
        assert p.status == PaymentStatus.PARTIAL

    def test_confirm_refund_accumulates_amount(self):
        p = MockPayment(status=PaymentStatus.REFUND_STARTED)
        p.amount_paid = 100
        create_payment_machine(p)
        p.confirm_refund(amount=30)
        assert p.status == PaymentStatus.PARTIAL
        assert p.amount_refunded == 30

    def test_confirm_refund_defaults_to_remaining_amount(self):
        p = MockPayment(status=PaymentStatus.REFUND_STARTED)
        p.amount_paid = 100
        p.amount_refunded = 40
        create_payment_machine(p)
        p.confirm_refund()
        assert p.amount_refunded == 100

    def test_mark_as_refunded_when_fully_refunded(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 100
        p.amount_refunded = 100
        create_payment_machine(p)
        p.mark_as_refunded()
        assert p.status == PaymentStatus.REFUNDED

    def test_mark_as_refunded_blocked_when_not_fully(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 100
        p.amount_refunded = 50
        create_payment_machine(p)
        from transitions.core import MachineError

        with pytest.raises(MachineError):
            p.mark_as_refunded()


class TestPaymentFail:
    def test_new_to_failed(self):
        p = MockPayment(status=PaymentStatus.NEW)
        create_payment_machine(p)
        p.fail()
        assert p.status == PaymentStatus.FAILED

    def test_pre_auth_to_failed(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.fail()
        assert p.status == PaymentStatus.FAILED

    def test_prepared_to_failed(self):
        p = MockPayment(status=PaymentStatus.PREPARED)
        create_payment_machine(p)
        p.fail()
        assert p.status == PaymentStatus.FAILED


# === Payment FSM: invalid transitions ===


class TestPaymentInvalidTransitions:
    def test_paid_cannot_fail(self):
        p = MockPayment(status=PaymentStatus.PAID)
        create_payment_machine(p)
        from transitions.core import MachineError

        with pytest.raises(MachineError):
            p.fail()

    def test_failed_cannot_prepare(self):
        p = MockPayment(status=PaymentStatus.FAILED)
        create_payment_machine(p)
        from transitions.core import MachineError

        with pytest.raises(MachineError):
            p.confirm_prepared()

    def test_refunded_cannot_charge(self):
        p = MockPayment(status=PaymentStatus.REFUNDED)
        create_payment_machine(p)
        from transitions.core import MachineError

        with pytest.raises(MachineError):
            p.confirm_charge_sent()


# === Fraud FSM ===


class TestFraudFSM:
    def test_unknown_to_rejected(self):
        p = MockPayment()
        create_fraud_machine(p)
        p.flag_as_fraud()
        assert p.fraud_status == FraudStatus.REJECTED

    def test_unknown_to_accepted(self):
        p = MockPayment()
        create_fraud_machine(p)
        p.flag_as_legit()
        assert p.fraud_status == FraudStatus.ACCEPTED

    def test_unknown_to_check(self):
        p = MockPayment()
        create_fraud_machine(p)
        p.flag_for_check()
        assert p.fraud_status == FraudStatus.CHECK

    def test_check_to_rejected(self):
        p = MockPayment(fraud_status=FraudStatus.CHECK)
        create_fraud_machine(p)
        p.mark_as_fraud()
        assert p.fraud_status == FraudStatus.REJECTED

    def test_check_to_accepted(self):
        p = MockPayment(fraud_status=FraudStatus.CHECK)
        create_fraud_machine(p)
        p.mark_as_legit()
        assert p.fraud_status == FraudStatus.ACCEPTED


# === ALLOWED_CALLBACKS ===


class TestAllowedCallbacks:
    def test_is_frozenset(self):
        assert isinstance(ALLOWED_CALLBACKS, frozenset)

    def test_contains_all_payment_triggers(self):
        expected = {
            "confirm_prepared",
            "confirm_lock",
            "confirm_charge_sent",
            "confirm_payment",
            "mark_as_paid",
            "release_lock",
            "start_refund",
            "cancel_refund",
            "confirm_refund",
            "mark_as_refunded",
            "fail",
        }
        assert expected == ALLOWED_CALLBACKS

    def test_does_not_contain_fraud_triggers(self):
        """Fraud triggers should not be externally invocable."""
        assert "flag_as_fraud" not in ALLOWED_CALLBACKS
        assert "flag_as_legit" not in ALLOWED_CALLBACKS
