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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["charge", "start_refund"])
@pytest.mark.parametrize(
    "amount",
    [
        Decimal(value)
        for value in ("-1", "0", "NaN", "sNaN", "Infinity", "-Infinity", "61")
    ]
    + [1, 1.5, "10"],
)
@pytest.mark.parametrize("via_validator", [False, True])
async def test_invalid_effective_amount_never_reaches_processor(
    recording_flow, mock_repo, operation, amount, via_validator
):
    flow, calls = recording_flow
    payment = MockPayment(
        status="pre-auth" if operation == "charge" else "partially_paid",
        amount_paid=Decimal("40") if operation == "charge" else Decimal("100"),
        amount_locked=Decimal("60") if operation == "charge" else Decimal("0"),
        amount_refunded=Decimal("0")
        if operation == "charge"
        else Decimal("40"),
    )
    if via_validator:

        def replace_amount(context):
            context["kwargs"]["amount"] = amount
            return context

        flow.validators = [replace_amount]

    with pytest.raises(InvalidTransitionError):
        await getattr(flow, operation)(
            payment, Decimal("10") if via_validator else amount
        )

    assert calls == []
    assert mock_repo.save_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["charge", "start_refund"])
@pytest.mark.parametrize("via_validator", [False, True])
async def test_none_uses_remaining_balance(
    recording_flow, operation, via_validator
):
    flow, calls = recording_flow
    payment = MockPayment(
        status="pre-auth" if operation == "charge" else "partially_paid",
        amount_paid=Decimal("40") if operation == "charge" else Decimal("100"),
        amount_locked=Decimal("60") if operation == "charge" else Decimal("0"),
        amount_refunded=Decimal("0")
        if operation == "charge"
        else Decimal("40"),
    )
    if via_validator:

        def default_amount(context):
            context["kwargs"].pop("amount")
            return context

        flow.validators = [default_amount]

    await getattr(flow, operation)(
        payment, Decimal("10") if via_validator else None
    )

    assert calls == [(operation, Decimal("60"))]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["charge", "start_refund"])
async def test_exhausted_balance_never_reaches_processor(
    recording_flow, operation
):
    flow, calls = recording_flow
    payment = MockPayment(
        status="pre-auth" if operation == "charge" else "refund_started",
        amount_paid=Decimal("100"),
        amount_refunded=Decimal("100"),
    )

    with pytest.raises(InvalidTransitionError):
        await getattr(flow, operation)(payment)

    assert calls == []
