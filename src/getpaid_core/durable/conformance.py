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
all survive, and unresolved work stays discoverable after the fact.

Passing proves the semantic contract against the adapter's own storage.
It proves nothing about a live provider, and nothing about behaviour
under a real database's isolation level that this suite does not
exercise.
"""

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from decimal import Decimal

from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.repository import DurablePaymentRepository
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import ConformanceError
from getpaid_core.exceptions import OperationConflictError
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


#: The checks an adapter must pass, in the order the suite runs them.
CONFORMANCE_CHECKS: tuple[
    tuple[str, Callable[[RepositoryFactory], Awaitable[None]]], ...
] = (
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
        "outstanding_operation_blocks_unrelated_commands",
        check_outstanding_operation_blocks_unrelated_commands,
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
