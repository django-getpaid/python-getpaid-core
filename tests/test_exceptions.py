"""Tests for getpaid_core.exceptions."""

import pytest

from getpaid_core.exceptions import BackendNotFoundError
from getpaid_core.exceptions import ChargeFailure
from getpaid_core.exceptions import CommunicationError
from getpaid_core.exceptions import CredentialsError
from getpaid_core.exceptions import GetPaidException
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import LockFailure
from getpaid_core.exceptions import ReconciliationRequiredError
from getpaid_core.exceptions import RefundFailure


class TestGetPaidException:
    def test_message(self):
        exc = GetPaidException("something went wrong")
        assert str(exc) == "something went wrong"

    def test_context_default(self):
        exc = GetPaidException("error")
        assert exc.context == {}

    def test_context_provided(self):
        ctx = {"order_id": "123"}
        exc = GetPaidException("error", context=ctx)
        assert exc.context == ctx
        assert exc.context["order_id"] == "123"

    def test_is_exception(self):
        with pytest.raises(GetPaidException):
            raise GetPaidException("test")


class TestExceptionHierarchy:
    """Verify the inheritance tree matches django-getpaid."""

    def test_communication_error_is_getpaid(self):
        assert issubclass(CommunicationError, GetPaidException)

    def test_charge_failure_is_communication(self):
        assert issubclass(ChargeFailure, CommunicationError)

    def test_lock_failure_is_communication(self):
        assert issubclass(LockFailure, CommunicationError)

    def test_refund_failure_is_communication(self):
        assert issubclass(RefundFailure, CommunicationError)

    def test_credentials_error_is_getpaid(self):
        assert issubclass(CredentialsError, GetPaidException)

    def test_invalid_callback_is_getpaid(self):
        assert issubclass(InvalidCallbackError, GetPaidException)

    def test_invalid_transition_is_getpaid(self):
        assert issubclass(InvalidTransitionError, GetPaidException)

    def test_backend_not_found_is_getpaid_and_keyerror(self):
        assert issubclass(BackendNotFoundError, GetPaidException)
        assert issubclass(BackendNotFoundError, KeyError)

    def test_backend_not_found_str_is_plain_message(self):
        exc = BackendNotFoundError("no backend for slug 'x'")
        assert str(exc) == "no backend for slug 'x'"

    def test_reconciliation_required_is_getpaid(self):
        assert issubclass(ReconciliationRequiredError, GetPaidException)

    def test_reconciliation_required_carries_charge_result(self):
        marker = object()
        exc = ReconciliationRequiredError(
            "manual reconciliation required", charge_result=marker
        )
        assert exc.charge_result is marker


class TestExceptionContext:
    """All exceptions support the context kwarg."""

    def test_communication_error_context(self):
        exc = CommunicationError("fail", context={"url": "/pay"})
        assert exc.context["url"] == "/pay"

    def test_charge_failure_context(self):
        exc = ChargeFailure("fail", context={"amount": 100})
        assert exc.context["amount"] == 100
