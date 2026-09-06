"""The reusable adapter conformance suite, and proof that it bites."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from getpaid_core.durable import CONFORMANCE_CHECKS
from getpaid_core.durable import DurablePaymentRepository
from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import ObservationPlan
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import plan_observation
from getpaid_core.durable import run_conformance_suite
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import ConformanceError
from getpaid_core.types import PaymentUpdate


async def in_memory_factory(facts: PaymentFacts) -> InMemoryDurableRepository:
    return InMemoryDurableRepository([facts])


class StaleSnapshotRepository(InMemoryDurableRepository):
    """The pre-ADR failure mode: plan on a snapshot, then save it.

    The read happens outside the boundary that commits, so an independent
    worker's committed funds can be overwritten by an older plan. It
    exists to prove the conformance suite detects that, and must never be
    used for anything else.
    """

    async def apply_observation(
        self, payment_id: str, update: PaymentUpdate | None
    ) -> ObservationPlan:
        stale = self._facts[payment_id]
        replay = self._replay.setdefault(payment_id, [])
        await asyncio.sleep(0)
        plan = plan_observation(stale, replay, update)
        async with self._lock_for(payment_id):
            self._facts[payment_id] = plan.facts
            if plan.replay_record is not None:
                self._replay[payment_id].append(plan.replay_record)
        return plan


async def stale_snapshot_factory(
    facts: PaymentFacts,
) -> StaleSnapshotRepository:
    return StaleSnapshotRepository([facts])


def prepared_facts() -> PaymentFacts:
    return PaymentFacts(
        payment_id="pay-1",
        amount_required=Decimal("100.00"),
        status=PaymentStatus.PREPARED,
    )


def test_the_suite_has_checks_for_every_required_race():
    names = {name for name, _ in CONFORMANCE_CHECKS}

    assert names == {
        "stale_capture_cannot_regress_funds",
        "capture_and_refund_race_preserves_both",
        "duplicate_events_are_idempotent",
        "distinct_events_all_survive",
        "unresolved_operations_are_discoverable",
        "reconciliation_flags_are_enumerable",
        "outstanding_operation_blocks_unrelated_commands",
        "metadata_cannot_forge_replay_history",
        "malformed_metadata_is_rejected_atomically",
        "reconciliation_blocks_new_commands",
        "submission_right_is_exclusive",
        "conflicting_outcomes_are_retained",
        "observations_commit_operations_and_disputes",
        "recovery_and_resolution_are_retained",
    }


async def test_conformance_rejects_dropped_observation_operations():
    class DroppedOperations(InMemoryDurableRepository):
        async def apply_observation(self, payment_id, update):
            operations = list(self._operations.get(payment_id, ()))
            plan = await super().apply_observation(payment_id, update)
            self._operations[payment_id] = operations
            return plan

    async def factory(facts):
        return DroppedOperations([facts])

    with pytest.raises(ConformanceError, match="observations_commit_operations"):
        await run_conformance_suite(factory)


async def test_reference_repository_passes_the_conformance_suite():
    await run_conformance_suite(in_memory_factory)


async def test_stale_snapshot_repository_fails_the_conformance_suite():
    with pytest.raises(ConformanceError) as excinfo:
        await run_conformance_suite(stale_snapshot_factory)

    message = str(excinfo.value)
    assert "stale_capture_cannot_regress_funds" in message
    assert "captured funds regressed to 40.00" in message


async def test_conformance_rejects_an_adapter_granting_duplicate_submission_rights():
    class DuplicateClaims(InMemoryDurableRepository):
        async def claim_submission(self, *args, **kwargs):
            plan = await super().claim_submission(*args, **kwargs)
            return replace(plan, granted=True)

    async def factory(facts):
        return DuplicateClaims([facts])

    with pytest.raises(ConformanceError, match="submission_right_is_exclusive"):
        await run_conformance_suite(factory)


async def test_conformance_rejects_an_adapter_discarding_conflicting_evidence():
    class DiscardingEvidence(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args, **kwargs):
            plan = await super().record_operation_outcome(*args, **kwargs)
            self._operations[plan.operation.payment_id] = [
                replace(record, conflicting_outcomes=())
                for record in self._operations[plan.operation.payment_id]
            ]
            return plan

    async def factory(facts):
        return DiscardingEvidence([facts])

    with pytest.raises(
        ConformanceError, match="conflicting_outcomes_are_retained"
    ):
        await run_conformance_suite(factory)


async def test_reference_repository_is_a_durable_repository():
    assert isinstance(InMemoryDurableRepository(), DurablePaymentRepository)


async def test_independent_workers_cannot_erase_committed_funds():
    """The scenario reported in the finding, through the new contract."""
    repository = InMemoryDurableRepository([prepared_facts()])

    def capture(amount: str, event_identity: str) -> PaymentUpdate:
        return PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal(amount),
            provider_event_id=event_identity,
        )

    await asyncio.gather(
        repository.apply_observation("pay-1", capture("100.00", "full")),
        repository.apply_observation("pay-1", capture("40.00", "partial")),
    )

    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == Decimal("100.00")
    assert facts.status == PaymentStatus.PAID

    replayed = await repository.apply_observation(
        "pay-1", capture("40.00", "partial")
    )
    assert replayed.applied is False


async def test_conflicting_event_identity_is_durably_flagged():
    """Reconciliation is discoverable from stored state, not a return value."""
    repository = InMemoryDurableRepository([prepared_facts()])
    applied = PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal("40.00"),
        provider_event_id="e-1",
    )
    conflicting = PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal("100.00"),
        provider_event_id="e-1",
    )

    await repository.apply_observation("pay-1", applied)
    plan = await repository.apply_observation("pay-1", conflicting)

    assert plan.applied is False
    facts = await repository.get_payment_facts("pay-1")
    assert facts.reconciliation_required is True
    assert facts.captured_funds == Decimal("40.00")

    flagged = await repository.list_payments_requiring_reconciliation()
    assert [entry.payment_id for entry in flagged] == ["pay-1"]
