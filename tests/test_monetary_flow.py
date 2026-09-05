"""Monetary preconditions tested through the flow and recording processors."""

from copy import deepcopy
from decimal import Decimal

import pytest

from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import ReconciliationRequiredError
from getpaid_core.flow import PaymentFlow
from getpaid_core.types import ChargeResult
from getpaid_core.types import RefundResult
from tests.conftest import MockPayment
from tests.conftest import MockProcessor


@pytest.fixture
def provider_results():
    return {}


@pytest.fixture
def recording_flow(mock_repo, mock_registry, provider_results):
    calls = []

    class RecordingProcessor(MockProcessor):
        async def charge(self, amount=None, **kwargs):
            calls.append(("charge", amount))
            return provider_results.get(
                "charge", ChargeResult(amount_charged=amount, success=True)
            )

        async def start_refund(self, amount=None, **kwargs):
            calls.append(("start_refund", amount))
            return provider_results.get(
                "start_refund", RefundResult(amount=amount)
            )

        async def release_lock(self, **kwargs):
            calls.append(("release_lock", None))
            return provider_results.get(
                "release_lock", self.payment.amount_locked
            )

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["charge", "start_refund", "release_lock"]
)
@pytest.mark.parametrize(
    "amount",
    [
        Decimal(value)
        for value in ("-1", "0", "NaN", "sNaN", "Infinity", "-Infinity", "41")
    ]
    + [None, 1, "10"],
)
async def test_invalid_provider_result_preserves_local_state(
    recording_flow, provider_results, mock_repo, operation, amount
):
    flow, calls = recording_flow
    payment = MockPayment(
        status="paid" if operation == "start_refund" else "pre-auth",
        amount_locked=Decimal("0")
        if operation == "start_refund"
        else Decimal("40"),
        amount_paid=Decimal("100")
        if operation == "start_refund"
        else Decimal("0"),
        provider_data={"original": True},
    )
    if operation == "charge":
        result = ChargeResult(
            amount_charged=amount, success=True, provider_data={"changed": True}
        )
    elif operation == "start_refund":
        result = RefundResult(amount=amount, provider_data={"changed": True})
    else:
        result = amount
    provider_results[operation] = result
    before = {**vars(payment), "provider_data": deepcopy(payment.provider_data)}

    with pytest.raises(ReconciliationRequiredError) as exc_info:
        if operation == "release_lock":
            await flow.release_lock(payment)
        else:
            await getattr(flow, operation)(payment, Decimal("40"))

    assert len(calls) == 1
    assert mock_repo.save_calls == 0
    assert vars(payment) == before
    assert isinstance(exc_info.value.__cause__, InvalidTransitionError)
    if operation == "charge":
        assert exc_info.value.charge_result is result
    else:
        assert exc_info.value.context["provider_result"] is result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "async_call"), [(False, False), (True, True)]
)
@pytest.mark.parametrize(
    "amount",
    [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("101")],
)
async def test_non_capture_charge_results_are_validated(
    recording_flow, provider_results, mock_repo, success, async_call, amount
):
    flow, calls = recording_flow
    payment = MockPayment(status="pre-auth", amount_locked=Decimal("100"))
    provider_results["charge"] = ChargeResult(
        amount, success=success, async_call=async_call
    )

    with pytest.raises(ReconciliationRequiredError):
        await flow.charge(payment)

    assert len(calls) == 1
    assert mock_repo.save_calls == 0
    assert payment.status == "pre-auth"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "async_call", "status"),
    [(False, False, "failed"), (True, True, "charge_started")],
)
async def test_zero_charge_result_supported_for_decline_or_pending(
    recording_flow, provider_results, success, async_call, status
):
    flow, calls = recording_flow
    payment = MockPayment(status="pre-auth", amount_locked=Decimal("100"))
    provider_results["charge"] = ChargeResult(
        Decimal("0"), success=success, async_call=async_call
    )

    await flow.charge(payment)

    assert calls == [("charge", Decimal("100"))]
    assert payment.status == status
    assert payment.amount_paid == Decimal("0")


@pytest.mark.asyncio
async def test_declined_charge_cannot_report_captured_funds(
    recording_flow, provider_results
):
    flow, calls = recording_flow
    payment = MockPayment(status="pre-auth", amount_locked=Decimal("100"))
    provider_results["charge"] = ChargeResult(Decimal("1"), success=False)

    with pytest.raises(ReconciliationRequiredError):
        await flow.charge(payment)

    assert len(calls) == 1
    assert payment.status == "pre-auth"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["charge", "start_refund", "release_lock"]
)
@pytest.mark.parametrize(
    "field",
    ["amount_required", "amount_paid", "amount_locked", "amount_refunded"],
)
@pytest.mark.parametrize(
    "amount",
    [
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        "100",
    ],
)
async def test_invalid_stored_balance_never_reaches_processor(
    recording_flow, mock_repo, operation, field, amount
):
    flow, calls = recording_flow
    payment = MockPayment(
        status="paid" if operation == "start_refund" else "pre-auth",
        amount_paid=Decimal("100")
        if operation == "start_refund"
        else Decimal("0"),
        amount_locked=Decimal("0")
        if operation == "start_refund"
        else Decimal("100"),
    )
    setattr(payment, field, amount)

    with pytest.raises(InvalidTransitionError):
        if operation == "release_lock":
            await flow.release_lock(payment)
        else:
            await getattr(flow, operation)(payment, Decimal("10"))

    assert calls == []
    assert mock_repo.save_calls == 0


@pytest.mark.asyncio
async def test_partial_release_result_does_not_clear_full_authorization(
    recording_flow, provider_results
):
    flow, calls = recording_flow
    payment = MockPayment(status="pre-auth", amount_locked=Decimal("100"))
    provider_results["release_lock"] = Decimal("40")

    with pytest.raises(ReconciliationRequiredError):
        await flow.release_lock(payment)

    assert len(calls) == 1
    assert payment.status == "pre-auth"
    assert payment.amount_locked == Decimal("100")
