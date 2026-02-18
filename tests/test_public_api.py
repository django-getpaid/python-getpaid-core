"""Tests for the public API surface of getpaid_core."""

import getpaid_core


class TestPublicAPI:
    def test_version(self):
        assert getpaid_core.__version__ == "0.1.1"

    def test_exports_enums(self):
        assert getpaid_core.PaymentStatus is not None
        assert getpaid_core.FraudStatus is not None
        assert getpaid_core.BackendMethod is not None
        assert getpaid_core.ConfirmationMethod is not None

    def test_exports_exceptions(self):
        assert getpaid_core.GetPaidException is not None
        assert getpaid_core.CommunicationError is not None
        assert getpaid_core.ChargeFailure is not None
        assert getpaid_core.LockFailure is not None
        assert getpaid_core.RefundFailure is not None
        assert getpaid_core.CredentialsError is not None
        assert getpaid_core.InvalidCallbackError is not None
        assert getpaid_core.InvalidTransitionError is not None

    def test_exports_core_classes(self):
        assert getpaid_core.BaseProcessor is not None
        assert getpaid_core.PaymentFlow is not None

    def test_exports_registry(self):
        assert getpaid_core.registry is not None

    def test_all_is_defined(self):
        assert hasattr(getpaid_core, "__all__")
        assert len(getpaid_core.__all__) > 0
