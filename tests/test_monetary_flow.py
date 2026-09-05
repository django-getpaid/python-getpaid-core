"""Monetary preconditions tested through the flow and recording processors."""

from copy import deepcopy
from decimal import Decimal

import pytest

from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.flow import PaymentFlow
from getpaid_core.types import ChargeResult
from getpaid_core.types import RefundResult
from tests.conftest import MockPayment
from tests.conftest import MockProcessor


@pytest.fixture
def recording_flow(mock_repo, mock_registry):
    calls = []

    class RecordingProcessor(MockProcessor):
        async def charge(self, amount=None, **kwargs):
            calls.append(("charge", amount))
            return ChargeResult(amount_charged=amount, success=True)

        async def start_refund(self, amount=None, **kwargs):
            calls.append(("start_refund", amount))
            return RefundResult(amount=amount)

    mock_registry.unregister("mock")
    mock_registry.register(RecordingProcessor)
    return PaymentFlow(mock_repo, registry=mock_registry), calls


@pytest.mark.asyncio
async def test_overcapture_never_reaches_processor(recording_flow, mock_repo):
    flow, calls = recording_flow
    payment = MockPayment(status="pre-auth", amount_locked=Decimal("100"))
    before = {**vars(payment), "provider_data": deepcopy(payment.provider_data)}

    with pytest.raises(InvalidTransitionError):
        await flow.charge(payment, Decimal("150"))

    assert calls == []
    assert mock_repo.save_calls == 0
    assert vars(payment) == before
