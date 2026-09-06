"""The flow boundary over a durable repository."""

import asyncio
from decimal import Decimal

import pytest

from getpaid_core.durable import DurablePaymentFlow
from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import UnsupportedRepositoryError
from getpaid_core.flow import PaymentFlow
from tests.conftest import MockPayment
from tests.conftest import MockRepository


@pytest.fixture
def durable_repo() -> InMemoryDurableRepository:
    return InMemoryDurableRepository(
        [
            PaymentFacts(
                payment_id="pay-1",
                amount_required=Decimal("100.00"),
                status=PaymentStatus.PREPARED,
            )
        ]
    )


@pytest.fixture
def durable_flow(durable_repo, mock_registry) -> DurablePaymentFlow:
    return DurablePaymentFlow(durable_repo, registry=mock_registry)


def confirmation(paid_amount: str, event_id: str) -> dict:
    return {
        "event": "payment_confirmed",
        "paid_amount": paid_amount,
        "event_id": event_id,
    }


async def test_callback_commits_to_current_state_and_returns_it(
    durable_flow, durable_repo
):
    stale = MockPayment(status=PaymentStatus.PREPARED)

    plan = await durable_flow.handle_callback(
        stale, confirmation("100.00", "full"), {}
    )

    assert plan.facts.captured_funds == Decimal("100.00")
    assert plan.facts.status == PaymentStatus.PAID
    committed = await durable_repo.get_payment_facts("pay-1")
    assert committed.captured_funds == Decimal("100.00")


async def test_callback_never_writes_the_caller_supplied_snapshot(
    durable_flow,
):
    stale = MockPayment(status=PaymentStatus.PREPARED)

    await durable_flow.handle_callback(
        stale, confirmation("100.00", "full"), {}
    )

    assert stale.amount_paid == Decimal("0")
    assert stale.status == PaymentStatus.PREPARED
    assert stale.provider_data == {}


async def test_independent_callbacks_cannot_erase_committed_funds(
    durable_flow, durable_repo
):
    """The reported finding, driven through the flow."""
    first = MockPayment(status=PaymentStatus.PREPARED)
    second = MockPayment(status=PaymentStatus.PREPARED)

    await asyncio.gather(
        durable_flow.handle_callback(
            first, confirmation("100.00", "full"), {}
        ),
        durable_flow.handle_callback(
            second, confirmation("40.00", "partial"), {}
        ),
    )

    committed = await durable_repo.get_payment_facts("pay-1")
    assert committed.captured_funds == Decimal("100.00")
    assert committed.status == PaymentStatus.PAID


async def test_polling_shares_the_same_atomic_boundary(durable_flow):
    stale = MockPayment(status=PaymentStatus.PREPARED)

    plan = await durable_flow.fetch_and_update_status(stale)

    assert plan.facts.captured_funds == Decimal("100.00")
    assert stale.amount_paid == Decimal("0")


async def test_an_unsupported_repository_is_refused_at_construction(
    mock_registry,
):
    with pytest.raises(UnsupportedRepositoryError, match="reserve_operation"):
        DurablePaymentFlow(MockRepository(), registry=mock_registry)


async def test_reserve_then_record_outcome_addresses_the_payment_by_identity(
    mock_registry,
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                payment_id="pay-1",
                amount_required=Decimal("100.00"),
                remaining_authorization=Decimal("100.00"),
                status=PaymentStatus.PRE_AUTH,
            )
        ]
    )
    flow = DurablePaymentFlow(repository, registry=mock_registry)
    stale = MockPayment(status=PaymentStatus.PRE_AUTH)
    intent = OperationIntent(
        operation_id="op-1", operation_type=OperationType.CHARGE
    )

    reserved = await flow.reserve_operation(stale, intent)
    assert reserved.resolved_amount == Decimal("100.00")

    plan = await flow.record_operation_outcome(
        stale, "op-1", OperationOutcome(state=OperationState.SUCCEEDED)
    )

    assert plan.facts.captured_funds == Decimal("100.00")
    assert stale.amount_paid == Decimal("0")


async def test_operation_validators_run_before_a_reservation_commits(
    durable_repo, mock_registry
):
    seen = []

    def record(context):
        seen.append(context["operation"])
        return context

    flow = DurablePaymentFlow(
        durable_repo, validators=[record], registry=mock_registry
    )
    stale = MockPayment(status=PaymentStatus.PREPARED)

    await flow.handle_callback(stale, confirmation("100.00", "full"), {})
    with pytest.raises(InvalidTransitionError):
        await flow.reserve_operation(
            stale,
            OperationIntent(
                operation_id="op-1", operation_type=OperationType.CHARGE
            ),
        )

    assert seen == ["callback", "reserve_operation"]


async def test_released_flow_is_untouched_by_the_durable_contract(
    mock_registry,
):
    """``PaymentFlow`` keeps the 3.x behaviour; it does not sniff adapters."""
    repository = MockRepository()
    payment = MockPayment(status=PaymentStatus.PREPARED)
    repository._payments["pay-1"] = payment
    flow = PaymentFlow(repository, registry=mock_registry)

    result = await flow.handle_callback(
        payment, confirmation("100.00", "full"), {}
    )

    assert result is None
    assert payment.amount_paid == Decimal("100.00")
    assert repository.save_calls == 1
