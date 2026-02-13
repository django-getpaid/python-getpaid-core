"""Tests for getpaid_core.enums."""

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import ConfirmationMethod
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus


class TestPaymentStatus:
    """Payment status enum values match django-getpaid for compat."""

    def test_new(self):
        assert PaymentStatus.NEW == "new"

    def test_prepared(self):
        assert PaymentStatus.PREPARED == "prepared"

    def test_pre_auth(self):
        assert PaymentStatus.PRE_AUTH == "pre-auth"

    def test_in_charge(self):
        assert PaymentStatus.IN_CHARGE == "charge_started"

    def test_partial(self):
        assert PaymentStatus.PARTIAL == "partially_paid"

    def test_paid(self):
        assert PaymentStatus.PAID == "paid"

    def test_failed(self):
        assert PaymentStatus.FAILED == "failed"

    def test_refund_started(self):
        assert PaymentStatus.REFUND_STARTED == "refund_started"

    def test_refunded(self):
        assert PaymentStatus.REFUNDED == "refunded"

    def test_member_count(self):
        assert len(PaymentStatus) == 9

    def test_is_str_subclass(self):
        assert isinstance(PaymentStatus.NEW, str)


class TestFraudStatus:
    """Fraud status enum values match django-getpaid for compat."""

    def test_unknown(self):
        assert FraudStatus.UNKNOWN == "unknown"

    def test_accepted(self):
        assert FraudStatus.ACCEPTED == "accepted"

    def test_rejected(self):
        assert FraudStatus.REJECTED == "rejected"

    def test_check(self):
        assert FraudStatus.CHECK == "check"

    def test_member_count(self):
        assert len(FraudStatus) == 4


class TestBackendMethod:
    def test_get(self):
        assert BackendMethod.GET == "GET"

    def test_post(self):
        assert BackendMethod.POST == "POST"

    def test_rest(self):
        assert BackendMethod.REST == "REST"

    def test_member_count(self):
        assert len(BackendMethod) == 3


class TestConfirmationMethod:
    def test_push(self):
        assert ConfirmationMethod.PUSH == "PUSH"

    def test_pull(self):
        assert ConfirmationMethod.PULL == "PULL"

    def test_member_count(self):
        assert len(ConfirmationMethod) == 2
