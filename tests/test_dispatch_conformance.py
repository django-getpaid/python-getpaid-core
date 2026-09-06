"""Independent ADR 0001 dispatch checks using recording, in-memory fakes.

These asyncio tests certify neither database atomicity nor real providers.
"""

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest

from getpaid_core import PluginRegistry
from getpaid_core.durable import DurablePaymentFlow
from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable.provider import LookupSemantics
from getpaid_core.durable.provider import OperationCapabilities
from getpaid_core.enums import PaymentEvent
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.types import PaymentUpdate
from tests.conftest import MockProcessor


NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
IDEMPOTENT = OperationCapabilities(
    idempotency_scope="merchant-account/capture",
    idempotency_window=timedelta(hours=1),
)


def make_flow(processor, *, repository=None, **options):
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(processor)
    if repository is None:
        repository = InMemoryDurableRepository(
            [
                PaymentFacts(
                    "payment",
                    Decimal("100"),
                    backend=processor.slug,
                    remaining_authorization=Decimal("100"),
                    status="pre-auth",
                )
            ]
        )
    return repository, DurablePaymentFlow(
        repository, registry=registry, **options
    )


@pytest.fixture
def recording_processor():
    class Recording(MockProcessor):
        operation_capabilities = dict.fromkeys(OperationType, IDEMPOTENT)
        submissions = []
        lookups = []
        outcomes = {}

        @classmethod
        async def submit_operation(cls, operation, *, config):
            cls.submissions.append(operation)
            return cls.outcomes.get(
                operation.operation_id,
                OperationOutcome(OperationState.PROVIDER_PENDING),
            )

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            cls.lookups.append(operation)
            return OperationOutcome(OperationState.UNKNOWN)

    return Recording


async def test_overlapping_duplicate_workers_obtain_only_one_submission_right():
    entered = asyncio.Event()
    release = asyncio.Event()
    submissions = []

    class Recording(MockProcessor):
        operation_capabilities = {OperationType.CHARGE: IDEMPOTENT}

        @classmethod
        async def submit_operation(cls, operation, *, config):
            submissions.append(operation)
            entered.set()
            await release.wait()
            return OperationOutcome(
                OperationState.SUCCEEDED, correlation="provider-capture-1"
            )

    both_reserved = asyncio.Event()
    duplicate_finished = asyncio.Event()
    results = []

    class RendezvousRepository(InMemoryDurableRepository):
        reservations = 0

        async def reserve_operation(self, payment_id, intent):
            reserved = await super().reserve_operation(payment_id, intent)
            self.reservations += 1
            if self.reservations == 2:
                both_reserved.set()
            # Both workers hold their own RESERVED snapshot before either
            # claims transmission. The real repository arbitrates the claim.
            await both_reserved.wait()
            return reserved

    repository = RendezvousRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                backend=Recording.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, first_worker = make_flow(Recording, repository=repository)
    _, second_worker = make_flow(Recording, repository=repository)

    async def execute(worker):
        result = await worker.execute_operation(
            "payment", OperationIntent("capture", OperationType.CHARGE), now=NOW
        )
        results.append(result)
        if result.outcome is OperationState.SUBMITTING:
            duplicate_finished.set()

    async with asyncio.timeout(5), asyncio.TaskGroup() as workers:
        workers.create_task(execute(first_worker))
        workers.create_task(execute(second_worker))
        await entered.wait()
        await duplicate_finished.wait()
        assert results[0].outcome is OperationState.SUBMITTING
        assert results[0].snapshot.captured_funds == Decimal("0")
        assert len(submissions) == 1
        release.set()

    assert results[1].outcome is OperationState.SUCCEEDED
    assert results[1].snapshot.captured_funds == Decimal("100")
    assert len(submissions) == 1
    assert submissions[0].submission_attempts == 1
    assert submissions[0].state is OperationState.SUBMITTING
    assert await repository.list_unresolved_operations() == ()


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        OperationIntent(
            "capture",
            OperationType.CHARGE,
            Decimal("31"),
            {"route": {"account": "original"}},
        ),
        OperationIntent(
            "capture",
            OperationType.RELEASE_LOCK,
            Decimal("30"),
            {"route": {"account": "original"}},
        ),
        OperationIntent(
            "capture",
            OperationType.CHARGE,
            Decimal("30"),
            {"route": {"account": "changed"}},
        ),
    ],
    ids=["amount", "operation-type", "nested-parameters"],
)
async def test_same_identity_with_altered_request_conflicts_before_submission(
    recording_processor,
    changed,
    terminal,
):
    if terminal:
        recording_processor.outcomes["capture"] = OperationOutcome(
            OperationState.SUCCEEDED
        )
    repository, flow = make_flow(recording_processor)
    original = OperationIntent(
        "capture",
        OperationType.CHARGE,
        Decimal("30"),
        {"route": {"account": "original"}},
    )
    accepted = await flow.execute_operation("payment", original, now=NOW)
    with pytest.raises(OperationConflictError):
        await flow.execute_operation("payment", changed, now=NOW)
    assert len(recording_processor.submissions) == 1
    assert (
        await repository.get_operation("payment", "capture")
        == accepted.operation
    )
    assert await repository.get_payment_facts("payment") == accepted.snapshot


@pytest.mark.parametrize(
    "operation_type", [OperationType.CHARGE, OperationType.START_REFUND]
)
async def test_omitted_amount_stays_frozen_after_an_intervening_observation(
    recording_processor,
    operation_type,
):
    refund = operation_type is OperationType.START_REFUND
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                backend=recording_processor.slug,
                captured_funds=Decimal("100") if refund else Decimal("0"),
                remaining_authorization=Decimal("0")
                if refund
                else Decimal("100"),
                status="paid" if refund else "pre-auth",
            )
        ]
    )
    _, flow = make_flow(recording_processor, repository=repository)
    await repository.reserve_operation(
        "payment", OperationIntent("original", operation_type)
    )
    update = (
        PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("30"),
        )
        if refund
        else PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("30"),
        )
    )
    await repository.apply_observation("payment", update)
    result = await flow.execute_operation(
        "payment", OperationIntent("original", operation_type), now=NOW
    )
    assert len(recording_processor.submissions) == 1
    assert recording_processor.submissions[0].resolved_amount == Decimal("100")
    assert result.operation.resolved_amount == Decimal("100")
    if refund:
        assert result.snapshot.refunded_funds == Decimal("30")
    else:
        assert result.snapshot.captured_funds == Decimal("30")


@pytest.mark.parametrize(
    "operation_type", [OperationType.CHARGE, OperationType.START_REFUND]
)
async def test_distinct_equal_partial_intents_keep_separate_provider_correlations(
    recording_processor,
    operation_type,
):
    refund = operation_type is OperationType.START_REFUND
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                backend=recording_processor.slug,
                captured_funds=Decimal("100") if refund else Decimal("0"),
                remaining_authorization=Decimal("0")
                if refund
                else Decimal("100"),
                status="paid" if refund else "pre-auth",
            )
        ]
    )
    _, flow = make_flow(recording_processor, repository=repository)
    recording_processor.outcomes = {
        "partial-one": OperationOutcome(
            OperationState.SUCCEEDED, correlation="remote-one"
        ),
        "partial-two": OperationOutcome(
            OperationState.SUCCEEDED, correlation="remote-two"
        ),
    }
    first = await flow.execute_operation(
        "payment",
        OperationIntent("partial-one", operation_type, Decimal("30")),
        now=NOW,
    )
    second = await flow.execute_operation(
        "payment",
        OperationIntent("partial-two", operation_type, Decimal("30")),
        now=NOW,
    )
    duplicate = await flow.execute_operation(
        "payment",
        OperationIntent("partial-one", operation_type, Decimal("30")),
        now=NOW,
    )
    assert len(recording_processor.submissions) == 2
    assert [
        entry.operation_id for entry in recording_processor.submissions
    ] == ["partial-one", "partial-two"]
    assert (
        recording_processor.submissions[0].idempotency_key
        != recording_processor.submissions[1].idempotency_key
    )
    assert first.operation.correlation == "remote-one"
    assert (
        await repository.get_operation("payment", "partial-one")
    ).correlation == "remote-one"
    assert (
        await repository.get_operation("payment", "partial-two")
    ).correlation == "remote-two"
    assert duplicate.operation.correlation == "remote-one"
    assert second.outcome is OperationState.SUCCEEDED
    if refund:
        assert second.snapshot.captured_funds == Decimal("100")
        assert second.snapshot.refunded_funds == Decimal("60")
        assert second.snapshot.remaining_authorization == Decimal("0")
    else:
        assert second.snapshot.captured_funds == Decimal("60")
        assert second.snapshot.refunded_funds == Decimal("0")
        assert second.snapshot.remaining_authorization == Decimal("40")
    assert duplicate.snapshot == second.snapshot


@pytest.mark.parametrize(
    "outcome", [OperationState.SUCCEEDED, OperationState.REJECTED]
)
@pytest.mark.parametrize(
    "operation_type,initial_captured,initial_hold,initial_status,expected_captured,expected_refunded,expected_hold",
    [
        (OperationType.PREPARE, "0", "0", "new", "0", "0", "0"),
        (OperationType.CHARGE, "0", "100", "pre-auth", "100", "0", "0"),
        (
            OperationType.RELEASE_LOCK,
            "30",
            "70",
            "partially_paid",
            "30",
            "0",
            "0",
        ),
        (OperationType.START_REFUND, "100", "0", "paid", "100", "100", "0"),
        (OperationType.CANCEL_REFUND, "100", "0", "paid", "100", "0", "0"),
    ],
    ids=["prepare", "charge", "release-lock", "start-refund", "cancel-refund"],
)
async def test_every_mutation_dispatches_its_reserved_intent_and_terminal_outcome(
    recording_processor,
    operation_type,
    outcome,
    initial_captured,
    initial_hold,
    initial_status,
    expected_captured,
    expected_refunded,
    expected_hold,
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                backend=recording_processor.slug,
                captured_funds=Decimal(initial_captured),
                remaining_authorization=Decimal(initial_hold),
                status=initial_status,
            )
        ]
    )
    _, flow = make_flow(recording_processor, repository=repository)
    parameters = {}
    if operation_type is OperationType.CANCEL_REFUND:
        await repository.reserve_operation(
            "payment",
            OperationIntent("refund-target", OperationType.START_REFUND),
        )
        await repository.record_operation_outcome(
            "payment",
            "refund-target",
            OperationOutcome(
                OperationState.PROVIDER_PENDING, correlation="provider-refund"
            ),
        )
        parameters = {"target_operation_id": "refund-target"}
    recording_processor.outcomes["command"] = OperationOutcome(
        outcome,
        correlation="provider-command",
        external_id="provider-payment"
        if operation_type is OperationType.PREPARE
        else None,
    )
    result = await flow.execute_operation(
        "payment",
        OperationIntent("command", operation_type, parameters=parameters),
        now=NOW,
    )
    assert result.outcome is outcome
    assert result.operation_id == "command"
    assert result.operation.correlation == "provider-command"
    assert len(recording_processor.submissions) == 1
    submitted = recording_processor.submissions[0]
    assert submitted.operation_type is operation_type
    assert submitted.state is OperationState.SUBMITTING
    assert submitted.submission_attempts == 1
    assert submitted.submitted_at == NOW
    assert submitted.retry_until == datetime(2026, 9, 6, 13, tzinfo=UTC)
    if operation_type is OperationType.CANCEL_REFUND:
        assert submitted.parameters == {
            "target_operation_id": "refund-target",
            "target_correlation": "provider-refund",
        }
    else:
        assert submitted.parameters == {}
    if outcome is OperationState.SUCCEEDED:
        assert result.snapshot.captured_funds == Decimal(expected_captured)
        assert result.snapshot.refunded_funds == Decimal(expected_refunded)
        assert result.snapshot.remaining_authorization == Decimal(expected_hold)
    else:
        assert result.snapshot.captured_funds == Decimal(initial_captured)
        assert result.snapshot.refunded_funds == Decimal("0")
        assert result.snapshot.remaining_authorization == Decimal(initial_hold)


@pytest.mark.parametrize(
    "elapsed,scope,window",
    [
        (timedelta(hours=1), "merchant-account/capture", timedelta(hours=1)),
        (
            timedelta(minutes=59, seconds=45),
            "merchant-account/capture",
            timedelta(hours=1),
        ),
        (timedelta(minutes=10), "different-merchant", timedelta(hours=1)),
        (
            timedelta(minutes=10),
            "merchant-account/capture",
            timedelta(minutes=5),
        ),
        (timedelta(hours=2), "merchant-account/capture", timedelta(hours=24)),
        (timedelta(minutes=10), None, None),
    ],
    ids=[
        "expired",
        "insufficient-call-budget",
        "scope-drift",
        "shortened-window",
        "extension-does-not-renew",
        "withdrawn-guarantee",
    ],
)
async def test_resubmission_refused_outside_original_and_current_guarantees(
    recording_processor,
    elapsed,
    scope,
    window,
):
    recording_processor.outcomes["capture"] = OperationOutcome(
        OperationState.UNKNOWN
    )
    repository, flow = make_flow(recording_processor)
    await flow.execute_operation(
        "payment", OperationIntent("capture", OperationType.CHARGE), now=NOW
    )
    recording_processor.operation_capabilities = {
        OperationType.CHARGE: OperationCapabilities(
            idempotency_scope=scope,
            idempotency_window=window,
            lookup_semantics=LookupSemantics.AUTHORITATIVE,
        )
    }
    _, restarted_flow = make_flow(recording_processor, repository=repository)
    result = await restarted_flow.reconcile_operation(
        "payment", "capture", now=NOW + elapsed, resubmit=True
    )
    assert result.outcome is OperationState.UNKNOWN
    assert result.snapshot.captured_funds == Decimal("0")
    assert result.operation.submission_attempts == 1
    assert result.operation.retry_until == datetime(2026, 9, 6, 13, tzinfo=UTC)
    assert len(recording_processor.submissions) == 1
    assert len(recording_processor.lookups) == 1
    unresolved = await repository.list_unresolved_operations()
    assert len(unresolved) == 1
    assert unresolved[0].operation_id == "capture"


async def test_restricted_provider_gets_one_attempt_and_remains_blocked(
    recording_processor,
):
    recording_processor.operation_capabilities = {
        OperationType.CHARGE: OperationCapabilities()
    }
    recording_processor.outcomes["capture"] = OperationOutcome(
        OperationState.UNKNOWN
    )
    repository, flow = make_flow(
        recording_processor,
        restricted_operations=frozenset({OperationType.CHARGE}),
    )
    await flow.execute_operation(
        "payment", OperationIntent("capture", OperationType.CHARGE), now=NOW
    )
    _, restarted_flow = make_flow(
        recording_processor,
        repository=repository,
        restricted_operations=frozenset({OperationType.CHARGE}),
    )
    result = await restarted_flow.reconcile_operation(
        "payment", "capture", now=NOW + timedelta(days=90), resubmit=True
    )
    duplicate = await restarted_flow.execute_operation(
        "payment", OperationIntent("capture", OperationType.CHARGE), now=NOW
    )
    with pytest.raises(OperationConflictError):
        await restarted_flow.execute_operation(
            "payment",
            OperationIntent("unrelated", OperationType.CHARGE),
            now=NOW,
        )
    assert result.outcome is duplicate.outcome is OperationState.UNKNOWN
    assert result.snapshot.captured_funds == Decimal("0")
    assert result.operation.submission_attempts == 1
    assert result.operation.retry_until is None
    assert len(recording_processor.submissions) == 1
    assert recording_processor.lookups == []
    assert await repository.get_operation("payment", "unrelated") is None


@pytest.mark.parametrize(
    "lookup", [LookupSemantics.UNSUPPORTED, LookupSemantics.AUTHORITATIVE]
)
async def test_crash_after_claim_before_transmission_cannot_be_blindly_retried(
    recording_processor,
    lookup,
):
    recording_processor.operation_capabilities = {
        OperationType.CHARGE: OperationCapabilities(lookup_semantics=lookup)
    }
    repository, _ = make_flow(
        recording_processor,
        restricted_operations=frozenset({OperationType.CHARGE}),
    )
    intent = OperationIntent("capture", OperationType.CHARGE)
    await repository.reserve_operation("payment", intent)
    claim = await repository.claim_submission(
        "payment", "capture", expected_attempt=0, now=NOW
    )
    assert claim.granted is True
    # The worker dies here, before calling any provider method. Only storage
    # survives; the new worker cannot distinguish this from response loss.
    _, restarted_flow = make_flow(
        recording_processor,
        repository=repository,
        restricted_operations=frozenset({OperationType.CHARGE}),
    )
    duplicate = await restarted_flow.execute_operation(
        "payment", intent, now=NOW
    )
    assert duplicate.outcome is OperationState.SUBMITTING
    result = await restarted_flow.reconcile_operation(
        "payment", "capture", now=NOW + timedelta(days=1), resubmit=True
    )
    assert result.outcome is (
        OperationState.SUBMITTING
        if lookup is LookupSemantics.UNSUPPORTED
        else OperationState.UNKNOWN
    )
    assert result.snapshot.captured_funds == Decimal("0")
    assert result.operation.submission_attempts == 1
    assert recording_processor.submissions == []
    assert len(await repository.list_unresolved_operations()) == 1


@pytest.mark.parametrize("historical_refunded,earlier_outcome", [
    (Decimal("0"), None), (Decimal("20"), None),
    (Decimal("20"), OperationState.REJECTED),
    (Decimal("20"), OperationState.SUCCEEDED),
])
@pytest.mark.parametrize("late_first", [False, True])
@pytest.mark.parametrize("callback_before_response", [False, True])
async def test_late_cancelled_refund_and_distinct_refund_are_both_counted(
    historical_refunded, earlier_outcome, late_first, callback_before_response
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                backend=MockProcessor.slug,
                captured_funds=Decimal("100"),
                refunded_funds=Decimal("0") if earlier_outcome else historical_refunded,
                status="paid" if earlier_outcome or not historical_refunded else "partially_refunded",
            )
        ]
    )
    if earlier_outcome is not None:
        await repository.reserve_operation("payment", OperationIntent(
            "old", OperationType.START_REFUND, amount=Decimal("10")))
        await repository.record_operation_outcome("payment", "old", OperationOutcome(
            earlier_outcome, correlation="old-refund"))
        await repository.apply_observation("payment", PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=historical_refunded,
        ))
    await repository.reserve_operation(
        "payment",
        OperationIntent("r1", OperationType.START_REFUND, amount=Decimal("30")),
    )
    await repository.record_operation_outcome(
        "payment",
        "r1",
        OperationOutcome(
            OperationState.PROVIDER_PENDING, correlation="refund-1"
        ),
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent(
            "cancel",
            OperationType.CANCEL_REFUND,
            parameters={"target_operation_id": "r1"},
        ),
    )
    await repository.record_operation_outcome(
        "payment",
        "cancel",
        OperationOutcome(OperationState.SUCCEEDED, correlation="cancel-1"),
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("r2", OperationType.START_REFUND, amount=Decimal("30")),
    )
    late = OperationOutcome(OperationState.SUCCEEDED, correlation="refund-1")
    second = OperationOutcome(OperationState.SUCCEEDED, correlation="refund-2")
    if callback_before_response:
        await repository.apply_observation(
            "payment",
            PaymentUpdate(
                payment_event=PaymentEvent.REFUND_CONFIRMED,
                refunded_amount=historical_refunded + Decimal("30"),
            ),
        )
    if late_first:
        await repository.record_operation_outcome("payment", "r1", late)
    await repository.record_operation_outcome("payment", "r2", second)
    await repository.record_operation_outcome("payment", "r1", late)
    await repository.record_operation_outcome("payment", "r2", second)
    facts = await repository.get_payment_facts("payment")
    assert facts.refunded_funds == historical_refunded + Decimal("60")
    assert facts.captured_funds == Decimal("100")
    assert facts.reconciliation_required is True
    assert (
        await repository.get_operation("payment", "r1")
    ).settled_amount == Decimal("30")
    assert (
        await repository.get_operation("payment", "r2")
    ).settled_amount == Decimal("30")


@pytest.mark.parametrize(
    "settled_amount", [Decimal("30"), Decimal("100")], ids=["partial", "full"]
)
@pytest.mark.parametrize(
    "settlement_first",
    [True, False],
    ids=[
        "settlement-before-cancel-response",
        "settlement-after-cancel-response",
    ],
)
async def test_refund_cancellation_race_never_erases_confirmed_returned_funds(
    settled_amount,
    settlement_first,
):
    cancel_entered = asyncio.Event()
    release_cancel = asyncio.Event()
    submissions = []

    class RacingRefund(MockProcessor):
        operation_capabilities = dict.fromkeys(OperationType, IDEMPOTENT)

        @classmethod
        async def submit_operation(cls, operation, *, config):
            submissions.append(operation)
            if operation.operation_type is OperationType.START_REFUND:
                return OperationOutcome(
                    OperationState.PROVIDER_PENDING, correlation="remote-refund"
                )
            cancel_entered.set()
            await release_cancel.wait()
            return OperationOutcome(
                OperationState.SUCCEEDED, correlation="remote-cancel"
            )

    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                backend=RacingRefund.slug,
                captured_funds=Decimal("100"),
                status="paid",
            )
        ]
    )
    _, refund_worker = make_flow(RacingRefund, repository=repository)
    _, cancellation_worker = make_flow(RacingRefund, repository=repository)
    await refund_worker.execute_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND),
        now=NOW,
    )
    settlement = OperationOutcome(
        OperationState.SUCCEEDED,
        settled_amount=settled_amount,
        correlation="remote-refund",
    )
    async with asyncio.timeout(5), asyncio.TaskGroup() as workers:
        cancellation = workers.create_task(
            cancellation_worker.execute_operation(
                "payment",
                OperationIntent(
                    "cancel",
                    OperationType.CANCEL_REFUND,
                    parameters={"target_operation_id": "refund"},
                ),
                now=NOW,
            )
        )
        await cancel_entered.wait()
        if settlement_first:
            await repository.record_operation_outcome(
                "payment", "refund", settlement
            )
        release_cancel.set()

    if not settlement_first:
        await repository.record_operation_outcome(
            "payment", "refund", settlement
        )
    facts = await repository.get_payment_facts("payment")
    assert cancellation.result().outcome is OperationState.SUCCEEDED
    assert facts.captured_funds == Decimal("100")
    assert facts.refunded_funds == settled_amount
    assert facts.remaining_authorization == Decimal("0")
    assert facts.status == (
        "partially_refunded" if settled_amount == Decimal("30") else "refunded"
    )
    assert [entry.operation_id for entry in submissions] == ["refund", "cancel"]
    assert submissions[1].parameters == {
        "target_operation_id": "refund",
        "target_correlation": "remote-refund",
    }
    assert (
        await repository.get_operation("payment", "refund")
    ).correlation == "remote-refund"
    assert (
        await repository.get_operation("payment", "cancel")
    ).correlation == "remote-cancel"
