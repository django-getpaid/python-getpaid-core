"""Cancellation-aware cleanup for durable money operations (ADR 0001, 3).

Task cancellation is not provider cancellation: these tests cancel at
deterministic barriers -- at the final local save after a provider result,
and while awaiting the provider -- and assert that cancellation propagates,
that safe known evidence is persisted within a finite cleanup bound, and
that an interrupted cleanup still leaves a discoverable recovery anchor.

Tested async-runtime scope: asyncio only. Nothing here establishes Trio or
AnyIO behaviour; core declares no support for those runtimes.
"""

import asyncio
from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import LookupSemantics
from getpaid_core.durable import OperationCapabilities
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from tests.conftest import MockPayment
from tests.conftest import MockProcessor
from tests.test_durable_dispatch import NOW
from tests.test_durable_dispatch import make_flow
from tests.test_operation_recovery import recovery_flow
from tests.test_operator_resolution import decision


def barrier_repository(*, blocks="response", retry="commit", retain="commit"):
    """A repository whose outcome writes stop at deterministic barriers.

    ``blocks`` selects which write is interrupted: the submitting worker's
    ``response``, which names its claimed attempt, or a ``query``, which
    names none. That write sets ``entered`` and blocks until the caller
    cancels it. The cleanup's replacement write sets ``retried`` and then
    behaves as ``retry``; ``retain`` does the same for the evidence
    retention the cleanup falls back to. Every write records the attempt
    it acknowledged in ``attempts``.
    """

    class Barrier(InMemoryDurableRepository):
        entered = asyncio.Event()
        retried = asyncio.Event()
        attempts: list[int | None] = []

        async def record_operation_outcome(self, *args, **kwargs):
            attempt = kwargs.get("response_attempt")
            Barrier.attempts.append(attempt)
            interrupted = (attempt is None) == (blocks == "query")
            if interrupted and not Barrier.entered.is_set():
                Barrier.entered.set()
                await asyncio.Event().wait()
            if interrupted:
                Barrier.retried.set()
                if retry == "fail":
                    raise OSError("cleanup write unavailable")
                if retry == "hang":
                    await asyncio.Event().wait()
            return await super().record_operation_outcome(*args, **kwargs)

        async def record_operation_failure(self, *args):
            if retain == "hang":
                await asyncio.Event().wait()
            return await super().record_operation_failure(*args)

    return Barrier


async def cancel_at(barrier, task):
    """Cancel ``task`` once it reaches ``barrier``, and collect the result."""
    await asyncio.wait_for(barrier.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def lingering_tasks():
    """Every task still on the loop besides the test's own.

    Cleanup owns its write, so once a cancelled call has returned there is
    nothing of core's left running: no detached save outlives it.
    """
    return {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    }


async def assert_discoverable(repository, operation_id="intent"):
    """The intent a restarted process must still find without an exception."""
    assert operation_id in {
        record.operation_id
        for record in await repository.list_unresolved_operations()
    }


async def assert_money_unchanged(repository, before):
    """No cancellation path may move money on its way out."""
    facts = await repository.get_payment_facts("pay")
    assert facts.captured_funds == before.captured_funds
    assert facts.refunded_funds == before.refunded_funds
    assert facts.remaining_authorization == before.remaining_authorization


@pytest.mark.parametrize("operation_type", list(OperationType))
async def test_cancellation_at_final_save_commits_evidence_and_propagates(
    operation_type,
):
    repository_type = barrier_repository()
    repository, flow, intent, calls = await recovery_flow(
        operation_type,
        OperationOutcome(OperationState.SUCCEEDED, correlation="provider-1"),
        repository_type,
    )
    task = asyncio.create_task(flow.execute_operation("pay", intent, now=NOW))
    await cancel_at(repository_type.entered, task)
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.SUCCEEDED
    assert operation.correlation == "provider-1"
    assert not operation.response_pending
    assert not operation.reconciliation_required
    # The interrupted write is retried once, under the same claimed attempt.
    assert repository_type.attempts == [1, 1]
    assert len(calls) == 1
    assert not lingering_tasks()


@pytest.mark.parametrize("operation_type", list(OperationType))
async def test_cancellation_awaiting_provider_records_unknown_and_propagates(
    operation_type,
):
    entered = asyncio.Event()

    async def block():
        entered.set()
        await asyncio.Event().wait()

    repository, flow, intent, calls = await recovery_flow(
        operation_type,
        OperationOutcome(OperationState.SUCCEEDED),
        before_response=block,
    )
    before = await repository.get_payment_facts("pay")
    task = asyncio.create_task(flow.execute_operation("pay", intent, now=NOW))
    await cancel_at(entered, task)
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.UNKNOWN
    assert not operation.response_pending
    assert operation.settled_amount is None
    await assert_discoverable(repository)
    await assert_money_unchanged(repository, before)
    # An unknown outcome is not permission to send the command again.
    resumed = await flow.execute_operation("pay", intent, now=NOW)
    assert resumed.outcome is OperationState.UNKNOWN
    assert len(calls) == 1
    assert not lingering_tasks()


@pytest.mark.parametrize("operation_type", list(OperationType))
async def test_failed_cleanup_write_retains_evidence_for_reconciliation(
    operation_type,
):
    repository_type = barrier_repository(retry="fail")
    repository, flow, intent, calls = await recovery_flow(
        operation_type,
        OperationOutcome(OperationState.SUCCEEDED, correlation="provider-1"),
        repository_type,
    )
    before = await repository.get_payment_facts("pay")
    task = asyncio.create_task(flow.execute_operation("pay", intent, now=NOW))
    await cancel_at(repository_type.entered, task)
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.SUBMITTING
    assert operation.reconciliation_required
    assert [evidence.correlation for evidence in operation.recovery_evidence] == [
        "provider-1"
    ]
    assert operation.recovery_evidence[0].state is OperationState.SUCCEEDED
    await assert_money_unchanged(repository, before)
    await assert_discoverable(repository)
    assert len(calls) == 1
    assert not lingering_tasks()


@pytest.mark.parametrize("operation_type", list(OperationType))
async def test_timed_out_cleanup_leaves_the_submission_anchor_discoverable(
    operation_type,
):
    repository_type = barrier_repository(retry="hang", retain="hang")
    repository, flow, intent, calls = await recovery_flow(
        operation_type,
        OperationOutcome(OperationState.SUCCEEDED, correlation="provider-1"),
        repository_type,
    )
    flow.recovery_timeout = 0.001
    task = asyncio.create_task(flow.execute_operation("pay", intent, now=NOW))
    await cancel_at(repository_type.entered, task)
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.SUBMITTING
    assert operation.response_pending
    assert not operation.recovery_evidence
    await assert_discoverable(repository)
    assert len(calls) == 1
    assert not lingering_tasks()


@pytest.mark.parametrize("operation_type", list(OperationType))
async def test_repeated_cancellation_stops_cleanup_without_orphan_saves(
    operation_type,
):
    repository_type = barrier_repository(retry="hang", retain="hang")
    repository, flow, intent, calls = await recovery_flow(
        operation_type,
        OperationOutcome(OperationState.SUCCEEDED, correlation="provider-1"),
        repository_type,
    )
    task = asyncio.create_task(flow.execute_operation("pay", intent, now=NOW))
    await asyncio.wait_for(repository_type.entered.wait(), timeout=2)
    task.cancel()
    # The second cancellation arrives while the cleanup write is in flight.
    await asyncio.wait_for(repository_type.retried.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.SUBMITTING
    assert not operation.recovery_evidence
    await assert_discoverable(repository)
    assert len(calls) == 1
    assert not lingering_tasks()


async def queried_charge(repository_type, *, before_answer=None):
    """An unknown charge whose provider declares an authoritative lookup."""

    class Queried(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                lookup_semantics=LookupSemantics.AUTHORITATIVE
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            return OperationOutcome(OperationState.UNKNOWN)

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            if before_answer is not None:
                await before_answer()
            return OperationOutcome(
                OperationState.SUCCEEDED, correlation="charge-1"
            )

    repository = repository_type(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=Queried.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, flow = make_flow(
        Queried,
        repository=repository,
        restricted_operations=frozenset(OperationType),
    )
    intent = OperationIntent("intent", OperationType.CHARGE)
    await flow.execute_operation("pay", intent, now=NOW)
    return repository, flow


async def test_cancelled_provider_query_settles_nothing_and_propagates():
    entered = asyncio.Event()

    async def block():
        entered.set()
        await asyncio.Event().wait()

    repository, flow = await queried_charge(
        InMemoryDurableRepository, before_answer=block
    )
    task = asyncio.create_task(
        flow.reconcile_operation("pay", "intent", now=NOW)
    )
    await cancel_at(entered, task)
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.UNKNOWN
    assert operation.correlation is None
    assert not operation.reconciliation_required
    assert (await repository.get_payment_facts("pay")).captured_funds == 0
    assert not lingering_tasks()


async def test_cancelled_query_commits_its_evidence_without_claiming_a_worker():
    repository_type = barrier_repository(blocks="query")
    repository, flow = await queried_charge(repository_type)
    task = asyncio.create_task(
        flow.reconcile_operation("pay", "intent", now=NOW)
    )
    await cancel_at(repository_type.entered, task)
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.SUCCEEDED
    assert operation.correlation == "charge-1"
    assert (await repository.get_payment_facts("pay")).captured_funds == 100
    # The submitting response acknowledged attempt 1; a query acknowledges
    # no producer, and the cleanup must not start acknowledging one.
    assert repository_type.attempts == [1, None, None]
    assert not lingering_tasks()


async def test_cancelled_operator_resolution_commits_nothing_and_retries():
    class BlockedResolution(InMemoryDurableRepository):
        entered = asyncio.Event()

        async def resolve_operation(self, *args, **kwargs):
            if not BlockedResolution.entered.is_set():
                BlockedResolution.entered.set()
                await asyncio.Event().wait()
            return await super().resolve_operation(*args, **kwargs)

    repository, flow = await queried_charge(BlockedResolution)
    before = await repository.get_operation("pay", "intent")
    facts = await repository.get_payment_facts("pay")
    task = asyncio.create_task(
        flow.resolve_operation(
            "pay",
            "intent",
            decision(),
            expected_operation=before,
            expected_facts=facts,
        )
    )
    await cancel_at(BlockedResolution.entered, task)
    assert await repository.get_operation("pay", "intent") == before
    assert (await repository.get_payment_facts("pay")).captured_funds == 0
    assert not lingering_tasks()
    # An audited decision has no provider effect to recover: the operator
    # commits the same resolution ID again.
    result = await flow.resolve_operation(
        "pay",
        "intent",
        decision(),
        expected_operation=before,
        expected_facts=facts,
    )
    assert result.outcome is OperationState.SUCCEEDED
    assert result.snapshot.captured_funds == 100


async def test_cancellation_before_submission_never_reaches_the_provider():
    class BlockedClaim(InMemoryDurableRepository):
        entered = asyncio.Event()

        async def claim_submission(self, *args, **kwargs):
            BlockedClaim.entered.set()
            await asyncio.Event().wait()

    repository, flow, intent, calls = await recovery_flow(
        OperationType.CHARGE,
        OperationOutcome(OperationState.SUCCEEDED),
        BlockedClaim,
    )
    task = asyncio.create_task(flow.execute_operation("pay", intent, now=NOW))
    await cancel_at(BlockedClaim.entered, task)
    assert calls == []
    operation = await repository.get_operation("pay", "intent")
    assert operation.state is OperationState.RESERVED
    await assert_discoverable(repository)
    assert not lingering_tasks()


async def test_cancelled_observation_commits_nothing_and_propagates():
    class BlockedObservation(InMemoryDurableRepository):
        entered = asyncio.Event()

        async def apply_observation(self, *args, **kwargs):
            BlockedObservation.entered.set()
            await asyncio.Event().wait()

    repository, flow = await queried_charge(BlockedObservation)
    task = asyncio.create_task(
        flow.handle_callback(
            MockPayment(id="pay"), {"event": "payment_confirmed"}, {}
        )
    )
    await cancel_at(BlockedObservation.entered, task)
    # An observation carries no local intent of its own: nothing is
    # committed, and the provider's redelivery is the recovery path.
    assert (await repository.get_payment_facts("pay")).captured_funds == 0
    assert (
        await repository.get_operation("pay", "intent")
    ).state is OperationState.UNKNOWN
    assert not lingering_tasks()
