"""Tests for getpaid_core.enums."""

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import ConfirmationMethod
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus


class TestPaymentStatus:
    def test_values(self) -> None:
        assert PaymentStatus.NEW == "new"
        assert PaymentStatus.PAID == "paid"


class TestFraudStatus:
    def test_values(self) -> None:
        assert FraudStatus.UNKNOWN == "unknown"
        assert FraudStatus.CHECK == "check"


class TestPaymentEvent:
    def test_values(self) -> None:
        assert PaymentEvent.PREPARED == "prepared"
        assert PaymentEvent.PAYMENT_CAPTURED == "payment_captured"
        assert PaymentEvent.REFUND_CONFIRMED == "refund_confirmed"


class TestFraudEvent:
    def test_values(self) -> None:
        assert FraudEvent.REVIEW == "review"
        assert FraudEvent.ACCEPT == "accept"
        assert FraudEvent.REJECT == "reject"


class TestBackendMethod:
    def test_values(self) -> None:
        assert BackendMethod.GET == "GET"
        assert BackendMethod.POST == "POST"
        assert BackendMethod.REST == "REST"


class TestConfirmationMethod:
    def test_values(self) -> None:
        assert ConfirmationMethod.PUSH == "PUSH"
        assert ConfirmationMethod.PULL == "PULL"
