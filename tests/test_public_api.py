"""Tests for the public API surface of getpaid_core."""

import getpaid_core


class TestPublicAPI:
    def test_version_is_exported(self) -> None:
        # The actual version value is checked dynamically against the
        # installed package metadata in tests/test_version.py -- never
        # hardcode a version string here, it goes stale on every release.
        assert isinstance(getpaid_core.__version__, str)
        assert getpaid_core.__version__

    def test_exports_enums(self) -> None:
        assert getpaid_core.PaymentStatus is not None
        assert getpaid_core.PaymentEvent is not None
        assert getpaid_core.FraudStatus is not None
        assert getpaid_core.FraudEvent is not None

    def test_exports_exceptions(self) -> None:
        assert getpaid_core.GetPaidException is not None
        assert getpaid_core.InvalidTransitionError is not None

    def test_exports_core_classes(self) -> None:
        assert getpaid_core.BaseProcessor is not None
        assert getpaid_core.PaymentFlow is not None

    def test_exports_registry(self) -> None:
        assert getpaid_core.registry is not None
