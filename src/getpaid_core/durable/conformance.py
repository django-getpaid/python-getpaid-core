"""Reusable conformance checks for durable repository adapters.

Adapters run this suite against their own storage. Every check drives the
repository through its public semantic operations from **independent
concurrent callers**, each holding its own detached snapshot, and reads
state back through the repository rather than through an object it handed
out earlier -- a shared mutable fake passes races it should fail, so it
is not evidence.

The callers here are concurrent tasks, not separate processes: what the
suite exercises is that no caller's read-plan-commit can be interleaved
into losing another's committed state. It cannot, on its own, provoke
every interleaving a real deployment produces, so an adapter whose
atomicity depends on a database must also run its own multi-process and
isolation-level tests.

The suite covers what ADR 0001 requires of this layer: a stale cumulative
capture cannot regress committed funds, a capture racing a refund keeps
both totals, duplicate event identities apply once while distinct ones
all survive, and unresolved work -- outstanding operations and payments
flagged for reconciliation alike -- stays discoverable after the fact.
It also checks who owns replay evidence: provider metadata must be unable
to seed or erase it, a malformed payload must be refused without costing
committed funds or history, and a payment awaiting reconciliation must
refuse new commands.

Passing proves the semantic contract against the adapter's own storage.
It proves nothing about a live provider, and nothing about behaviour
under a real database's isolation level that this suite does not
exercise.
"""

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any
from typing import cast

from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import PaymentObservation
from getpaid_core.durable.repository import DurablePaymentRepository
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import ConformanceError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.exceptions import ReconciliationBlockedError
from getpaid_core.types import PaymentUpdate


#: An adapter-supplied factory returning a repository holding exactly the
#: given payment facts and no operations or replay evidence.
RepositoryFactory = Callable[
    [PaymentFacts], Awaitable[DurablePaymentRepository]
]

PAYMENT_ID = "conformance-payment"
REQUIRED = Decimal("100.00")


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise ConformanceError(detail)


def _prepared_facts() -> PaymentFacts:
    return PaymentFacts(
        payment_id=PAYMENT_ID,
        amount_required=REQUIRED,
        status=PaymentStatus.PREPARED,
    )


def _authorized_facts() -> PaymentFacts:
    return PaymentFacts(
        payment_id=PAYMENT_ID,
        amount_required=REQUIRED,
        remaining_authorization=REQUIRED,
        status=PaymentStatus.PRE_AUTH,
    )


def _capture(amount: str, event_identity: str) -> PaymentUpdate:
    return PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal(amount),
        provider_event_id=event_identity,
    )


def _refund(amount: str, event_identity: str) -> PaymentUpdate:
    return PaymentUpdate(
        payment_event=PaymentEvent.REFUND_CONFIRMED,
        refunded_amount=Decimal(amount),
        provider_event_id=event_identity,
    )


async def check_stale_capture_cannot_regress_funds(
    factory: RepositoryFactory,
) -> None:
    """A full and a stale partial capture race; the full one stands."""
    repository = await factory(_prepared_facts())

    await asyncio.gather(
        repository.apply_observation(PAYMENT_ID, _capture("100.00", "full")),
        repository.apply_observation(PAYMENT_ID, _capture("40.00", "partial")),
    )

    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts.captured_funds == Decimal("100.00"),
        f"captured funds regressed to {facts.captured_funds}",
    )
    for amount, identity in (("100.00", "full"), ("40.00", "partial")):
        replayed = await repository.apply_observation(
            PAYMENT_ID, _capture(amount, identity)
        )
        _require(
            not replayed.applied,
            f"replay evidence for {identity!r} did not survive the race",
        )


async def check_capture_and_refund_race_preserves_both(
    factory: RepositoryFactory,
) -> None:
    """A stale capture racing a refund erases neither total."""
    repository = await factory(
        PaymentFacts(
            payment_id=PAYMENT_ID,
            amount_required=REQUIRED,
            captured_funds=REQUIRED,
            status=PaymentStatus.PAID,
        )
    )

    await asyncio.gather(
        repository.apply_observation(PAYMENT_ID, _capture("40.00", "stale")),
        repository.apply_observation(PAYMENT_ID, _refund("40.00", "refund")),
    )

    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts.captured_funds == REQUIRED,
        f"captured funds regressed to {facts.captured_funds}",
    )
    _require(
        facts.refunded_funds == Decimal("40.00"),
        f"refunded funds lost: {facts.refunded_funds}",
    )


async def check_duplicate_events_are_idempotent(
    factory: RepositoryFactory,
) -> None:
    """The same event delivered twice moves money once."""
    repository = await factory(_prepared_facts())

    plans = await asyncio.gather(
        repository.apply_observation(PAYMENT_ID, _capture("100.00", "once")),
        repository.apply_observation(PAYMENT_ID, _capture("100.00", "once")),
    )

    applied = [plan for plan in plans if plan.applied]
    _require(
        len(applied) == 1,
        f"a duplicate event identity applied {len(applied)} times",
    )
    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts.captured_funds == REQUIRED,
        f"duplicate delivery changed captured funds to {facts.captured_funds}",
    )


async def check_distinct_events_all_survive(
    factory: RepositoryFactory,
) -> None:
    """Two distinct events both leave committed replay evidence."""
    repository = await factory(_prepared_facts())

    await asyncio.gather(
        repository.apply_observation(PAYMENT_ID, _capture("40.00", "e-1")),
        repository.apply_observation(PAYMENT_ID, _capture("100.00", "e-2")),
    )

    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts.captured_funds == REQUIRED,
        f"captured funds regressed to {facts.captured_funds}",
    )
    for amount, identity in (("40.00", "e-1"), ("100.00", "e-2")):
        replayed = await repository.apply_observation(
            PAYMENT_ID, _capture(amount, identity)
        )
        _require(
            not replayed.applied,
            f"replay evidence for {identity!r} was lost",
        )


async def check_unresolved_operations_are_discoverable(
    factory: RepositoryFactory,
) -> None:
    """An unresolved operation is findable without a caller object."""
    repository = await factory(_authorized_facts())
    intent = OperationIntent(
        operation_id="op-1", operation_type=OperationType.CHARGE
    )

    reserved = await repository.reserve_operation(PAYMENT_ID, intent)
    _require(
        reserved.resolved_amount == REQUIRED,
        f"reservation resolved {reserved.resolved_amount}, expected {REQUIRED}",
    )

    resumed = await repository.reserve_operation(PAYMENT_ID, intent)
    _require(
        resumed.operation_id == reserved.operation_id
        and resumed.resolved_amount == reserved.resolved_amount,
        "repeating the same intent did not resume its reservation",
    )

    await repository.record_operation_outcome(
        PAYMENT_ID, "op-1", OperationOutcome(state=OperationState.UNKNOWN)
    )
    unresolved = await repository.list_unresolved_operations()
    _require(
        any(record.operation_id == "op-1" for record in unresolved),
        "an unknown outcome left no discoverable unresolved work",
    )

    settled = await repository.record_operation_outcome(
        PAYMENT_ID, "op-1", OperationOutcome(state=OperationState.SUCCEEDED)
    )
    _require(
        settled.facts.captured_funds == REQUIRED,
        f"settlement recorded {settled.facts.captured_funds} captured funds",
    )
    unresolved = await repository.list_unresolved_operations()
    _require(
        all(record.operation_id != "op-1" for record in unresolved),
        "a settled operation is still reported as unresolved work",
    )


async def check_conflicting_outcomes_are_retained(
    factory: RepositoryFactory,
) -> None:
    """Distinct disputes survive concurrent writes, reads and redelivery."""
    repository = await factory(_authorized_facts())
    await repository.reserve_operation(
        PAYMENT_ID,
        OperationIntent("op-1", OperationType.CHARGE, amount=Decimal("40")),
    )
    confirmed = OperationOutcome(
        OperationState.SUCCEEDED, Decimal("20"), "capture-1"
    )
    completed = await repository.record_operation_outcome(
        PAYMENT_ID, "op-1", confirmed
    )
    disputed = (
        OperationOutcome(OperationState.SUCCEEDED, Decimal("30"), "capture-1"),
        OperationOutcome(OperationState.SUCCEEDED, Decimal("40"), "capture-1"),
        OperationOutcome(OperationState.SUCCEEDED, Decimal("20"), "capture-2"),
        OperationOutcome(OperationState.REJECTED, correlation="capture-1"),
    )
    await asyncio.gather(
        *(
            repository.record_operation_outcome(PAYMENT_ID, "op-1", evidence)
            for evidence in disputed
        )
    )
    for evidence in (*disputed, confirmed):
        await repository.record_operation_outcome(PAYMENT_ID, "op-1", evidence)

    stored = await repository.get_operation(PAYMENT_ID, "op-1")
    _require(stored is not None, "the disputed operation was lost")
    assert stored is not None
    _require(
        len(stored.conflicting_outcomes) == len(disputed)
        and all(
            evidence in stored.conflicting_outcomes for evidence in disputed
        ),
        "distinct conflicting outcomes were lost or duplicated in storage",
    )
    _require(
        stored
        == replace(
            completed.operation,
            reconciliation_required=True,
            conflicting_outcomes=stored.conflicting_outcomes,
        ),
        "conflicting evidence overwrote established operation facts",
    )
    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts == replace(completed.facts, reconciliation_required=True),
        "conflicting evidence changed financial facts or lost reconciliation",
    )
    _require(
        stored in await repository.list_unresolved_operations(),
        "the disputed terminal operation is not discoverable",
    )


async def check_reconciliation_flags_are_enumerable(
    factory: RepositoryFactory,
) -> None:
    """A payment flagged without any operation is still findable."""
    repository = await factory(_prepared_facts())
    applied = _capture("40.00", "e-1")
    conflicting = _capture("100.00", "e-1")

    await repository.apply_observation(PAYMENT_ID, applied)
    plan = await repository.apply_observation(PAYMENT_ID, conflicting)
    _require(
        not plan.applied,
        "a conflicting event identity was applied as a financial change",
    )

    flagged = await repository.list_payments_requiring_reconciliation()
    _require(
        any(facts.payment_id == PAYMENT_ID for facts in flagged),
        "a payment flagged for reconciliation is not enumerable",
    )


async def check_metadata_cannot_forge_replay_history(
    factory: RepositoryFactory,
) -> None:
    """Provider metadata can neither seed nor erase trusted evidence.

    An adapter that keeps replay records in the same mapping it merges
    processor metadata into fails here: the forged identity suppresses
    the genuine capture that follows it.
    """
    repository = await factory(_prepared_facts())

    await repository.apply_observation(
        PAYMENT_ID,
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("40.00"),
            provider_event_id="first",
            provider_data={"applied_event_ids": ["future"]},
        ),
    )
    genuine = await repository.apply_observation(
        PAYMENT_ID, _capture("100.00", "future")
    )
    _require(
        genuine.applied,
        "forged metadata suppressed a genuine capture",
    )

    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts.captured_funds == REQUIRED,
        f"forged metadata left captured funds at {facts.captured_funds}",
    )

    await repository.apply_observation(
        PAYMENT_ID,
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=REQUIRED,
            provider_event_id="erasing",
            provider_data={"applied_event_ids": []},
        ),
    )
    replayed = await repository.apply_observation(
        PAYMENT_ID, _capture("100.00", "future")
    )
    _require(
        not replayed.applied,
        "metadata erased committed replay evidence",
    )


async def check_malformed_metadata_is_rejected_atomically(
    factory: RepositoryFactory,
) -> None:
    """A refused observation loses neither funds nor committed history."""
    repository = await factory(_prepared_facts())
    await repository.apply_observation(PAYMENT_ID, _capture("40.00", "e-1"))

    # Annotated as ``dict[str, Any]`` and violated on purpose: the check
    # exists because a plugin building metadata at runtime can produce a
    # key no annotation stopped it from producing.
    malformed = cast("dict[str, Any]", {1: "not a string key"})

    try:
        await repository.apply_observation(
            PAYMENT_ID,
            PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=REQUIRED,
                provider_event_id="e-2",
                provider_data=malformed,
            ),
        )
    except InvalidTransitionError:
        pass
    else:
        raise ConformanceError("malformed metadata was accepted")

    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(
        facts.captured_funds == Decimal("40.00"),
        f"a rejected observation moved funds to {facts.captured_funds}",
    )
    replayed = await repository.apply_observation(
        PAYMENT_ID, _capture("40.00", "e-1")
    )
    _require(
        not replayed.applied,
        "a rejected observation lost committed replay evidence",
    )


async def check_reconciliation_blocks_new_commands(
    factory: RepositoryFactory,
) -> None:
    """A payment awaiting reconciliation refuses to reserve new work.

    This is the state a migrated ambiguous record lands in, and the one
    contradictory evidence produces. Outstanding operations still
    resolve; only new commands are refused.
    """
    repository = await factory(
        PaymentFacts(
            payment_id=PAYMENT_ID,
            amount_required=REQUIRED,
            remaining_authorization=REQUIRED,
            status=PaymentStatus.PRE_AUTH,
            reconciliation_required=True,
        )
    )

    try:
        await repository.reserve_operation(
            PAYMENT_ID,
            OperationIntent(
                operation_id="op-1", operation_type=OperationType.CHARGE
            ),
        )
    except ReconciliationBlockedError:
        return
    raise ConformanceError(
        "a payment awaiting reconciliation reserved a new command"
    )


async def check_outstanding_operation_blocks_unrelated_commands(
    factory: RepositoryFactory,
) -> None:
    """A second unrelated command is refused while one is outstanding."""
    repository = await factory(_authorized_facts())
    await repository.reserve_operation(
        PAYMENT_ID,
        OperationIntent(
            operation_id="op-1", operation_type=OperationType.CHARGE
        ),
    )

    try:
        await repository.reserve_operation(
            PAYMENT_ID,
            OperationIntent(
                operation_id="op-2", operation_type=OperationType.CHARGE
            ),
        )
    except OperationConflictError:
        return
    raise ConformanceError(
        "an unrelated command was reserved while another was outstanding"
    )


async def check_submission_right_is_exclusive(
    factory: RepositoryFactory,
) -> None:
    """Independent workers cannot both acquire the same submission attempt."""
    repository = await factory(_authorized_facts())
    intent = OperationIntent("submit-once", OperationType.CHARGE)
    first, second = await asyncio.gather(
        repository.reserve_operation(PAYMENT_ID, intent),
        repository.reserve_operation(PAYMENT_ID, intent),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claims = await asyncio.gather(
        *(
            repository.claim_submission(
                PAYMENT_ID,
                record.operation_id,
                expected_attempt=record.submission_attempts,
                now=now,
                retry_until=now + timedelta(hours=1),
                idempotency_scope="conformance",
            )
            for record in (first, second)
        )
    )
    _require(
        sum(claim.granted for claim in claims) == 1,
        "duplicate workers obtained independent submission rights",
    )
    stored = await repository.get_operation(PAYMENT_ID, "submit-once")
    _require(
        stored is not None and stored.state is OperationState.SUBMITTING,
        "submission was not durably marked before provider I/O",
    )
    _require(
        stored is not None and stored.submission_attempts == 1,
        "submission attempt counter was not committed atomically",
    )
    expired = await repository.claim_submission(
        PAYMENT_ID,
        "submit-once",
        expected_attempt=1,
        now=now + timedelta(hours=2),
    )
    _require(not expired.granted, "expiry authorized blind resubmission")


async def check_observations_commit_operations_and_disputes(
    factory: RepositoryFactory,
) -> None:
    """Commit correlated operations and rejected evidence with observations."""
    repository = await factory(_authorized_facts())
    await repository.reserve_operation(
        PAYMENT_ID, OperationIntent("capture", OperationType.CHARGE)
    )
    callback = PaymentObservation(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=REQUIRED,
        operation_id="capture",
        outcome=OperationOutcome(OperationState.SUCCEEDED),
        provider_event_id="callback",
    )
    await repository.apply_observation(PAYMENT_ID, callback)
    stored = await repository.get_operation(PAYMENT_ID, "capture")
    _require(
        stored is not None and stored.state is OperationState.SUCCEEDED,
        "callback completion was not committed with financial facts",
    )
    await repository.record_operation_outcome(
        PAYMENT_ID, "capture", OperationOutcome(OperationState.PROVIDER_PENDING)
    )
    replayed = await repository.apply_observation(PAYMENT_ID, callback)
    _require(not replayed.applied, "callback replay evidence was not committed")
    await repository.apply_observation(
        PAYMENT_ID, _capture("1000.00", "impossible")
    )
    facts = await repository.get_payment_facts(PAYMENT_ID)
    _require(facts.captured_funds == REQUIRED, "impossible money was committed")
    _require(
        facts.reconciliation_required and bool(facts.observation_conflicts),
        "disputed observation was not retained with reconciliation requirement",
    )
    _require(
        "1000" in facts.observation_conflicts[0].semantic_content,
        "disputed observation lost its financial claim",
    )


#: The checks an adapter must pass, in the order the suite runs them.
CONFORMANCE_CHECKS: tuple[
    tuple[str, Callable[[RepositoryFactory], Awaitable[None]]], ...
] = (
    (
        "submission_right_is_exclusive",
        check_submission_right_is_exclusive,
    ),
    (
        "stale_capture_cannot_regress_funds",
        check_stale_capture_cannot_regress_funds,
    ),
    (
        "capture_and_refund_race_preserves_both",
        check_capture_and_refund_race_preserves_both,
    ),
    (
        "duplicate_events_are_idempotent",
        check_duplicate_events_are_idempotent,
    ),
    (
        "distinct_events_all_survive",
        check_distinct_events_all_survive,
    ),
    (
        "unresolved_operations_are_discoverable",
        check_unresolved_operations_are_discoverable,
    ),
    (
        "conflicting_outcomes_are_retained",
        check_conflicting_outcomes_are_retained,
    ),
    (
        "reconciliation_flags_are_enumerable",
        check_reconciliation_flags_are_enumerable,
    ),
    (
        "outstanding_operation_blocks_unrelated_commands",
        check_outstanding_operation_blocks_unrelated_commands,
    ),
    (
        "metadata_cannot_forge_replay_history",
        check_metadata_cannot_forge_replay_history,
    ),
    (
        "malformed_metadata_is_rejected_atomically",
        check_malformed_metadata_is_rejected_atomically,
    ),
    (
        "reconciliation_blocks_new_commands",
        check_reconciliation_blocks_new_commands,
    ),
    (
        "observations_commit_operations_and_disputes",
        check_observations_commit_operations_and_disputes,
    ),
)


async def run_conformance_suite(factory: RepositoryFactory) -> None:
    """Run every conformance check against an adapter's repository.

    ``factory`` is awaited once per check and must return a repository
    holding exactly the payment facts it is given. Raises
    ``ConformanceError`` naming the first check that failed; any other
    exception is wrapped in one so the failing check is always named.
    """
    for name, check in CONFORMANCE_CHECKS:
        try:
            await check(factory)
        except ConformanceError as exc:
            raise ConformanceError(
                f"{name}: {exc}", context={"check": name}
            ) from exc
        except Exception as exc:
            raise ConformanceError(
                f"{name}: raised {type(exc).__name__}: {exc}",
                context={"check": name},
            ) from exc
