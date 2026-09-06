"""Recovery anchors survive secondary storage failure and unrelated writers."""

import asyncio
from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentObservation
from getpaid_core.exceptions import OperationPersistenceError
from tests.test_durable_dispatch import NOW
from tests.test_operation_recovery import recovery_flow
from tests.test_operator_resolution import decision


@pytest.mark.parametrize("operation_type", list(OperationType))
@pytest.mark.parametrize("recovery_failure", ["unavailable", "timeout"])
async def test_unavailable_recovery_storage_preserves_original_failure_and_submission_anchor(
    operation_type, recovery_failure
):
    failure = OSError("final write unavailable")

    class Unavailable(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args, **kwargs):
            raise failure

        async def record_operation_failure(self, *args):
            if recovery_failure == "timeout":
                await asyncio.Event().wait()
            raise OSError("secondary error containing secret payload")

    repository, flow, intent, calls = await recovery_flow(
        operation_type, OperationOutcome(OperationState.SUCCEEDED), Unavailable
    )
    flow.recovery_timeout = 0.001
    with pytest.raises(OperationPersistenceError) as caught:
        await flow.execute_operation("pay", intent, now=NOW)
    assert caught.value.__cause__ is failure
    assert caught.value.context["recovery_recorded"] is False
    assert "secret" not in repr(caught.value.context)
    assert (
        await repository.get_operation("pay", "intent")
    ).state is OperationState.SUBMITTING
    assert any(
        record.operation_id == "intent"
        for record in await repository.list_unresolved_operations()
    )
    await flow.execute_operation("pay", intent, now=NOW)
    assert len(calls) == 1


@pytest.mark.parametrize("operation_type", list(OperationType))
@pytest.mark.parametrize(
    "state",
    [
        OperationState.REJECTED,
        OperationState.PROVIDER_PENDING,
        OperationState.UNKNOWN,
    ],
)
async def test_ordinary_outcomes_are_not_local_recording_failures_or_settlement(
    operation_type, state
):
    repository, flow, intent, calls = await recovery_flow(
        operation_type, OperationOutcome(state, correlation="provider-1")
    )
    before = await repository.get_payment_facts("pay")
    result = await flow.execute_operation("pay", intent, now=NOW)
    assert result.outcome is state
    assert not result.operation.recovery_evidence
    assert result.snapshot.captured_funds == before.captured_funds
    assert result.snapshot.refunded_funds == before.refunded_funds
    assert (
        result.snapshot.remaining_authorization
        == before.remaining_authorization
    )
    assert await flow.execute_operation("pay", intent, now=NOW) == result
    assert len(calls) == 1


async def test_recovery_then_operator_resolution_retains_evidence_and_clears_operation_flag():
    class FailedOnce(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args, **kwargs):
            raise OSError("storage failure")

    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE,
        OperationOutcome(OperationState.SUCCEEDED),
        FailedOnce,
    )
    with pytest.raises(OperationPersistenceError):
        await flow.execute_operation("pay", intent, now=NOW)
    before = await repository.get_operation("pay", "intent")
    result = await flow.resolve_operation(
        "pay",
        "intent",
        decision(),
        expected_operation=before,
        expected_facts=await repository.get_payment_facts("pay"),
    )
    assert result.operation.recovery_evidence == before.recovery_evidence
    assert not result.reconciliation_required
    assert await repository.list_unresolved_operations() == ()


async def test_lost_commit_acknowledgement_does_not_downgrade_callback_or_resubmit():
    class LostAcknowledgement(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args, **kwargs):
            await super().record_operation_outcome(*args, **kwargs)
            raise OSError("committed, acknowledgement lost")

    repository, flow, intent, calls = await recovery_flow(
        OperationType.CHARGE,
        OperationOutcome(OperationState.SUCCEEDED),
        LostAcknowledgement,
    )
    with pytest.raises(OperationPersistenceError):
        await flow.execute_operation("pay", intent, now=NOW)
    record = await repository.get_operation("pay", "intent")
    assert record.state is OperationState.SUCCEEDED
    assert record.reconciliation_required
    assert (
        await repository.get_payment_facts("pay")
    ).captured_funds == Decimal("100")
    duplicate = await flow.execute_operation("pay", intent, now=NOW)
    assert duplicate.outcome is OperationState.SUCCEEDED
    assert len(calls) == 1


async def test_callback_completion_cannot_hide_unrecorded_response_after_storage_outage():
    from getpaid_core.durable import OperationCapabilities
    from getpaid_core.durable import OperationIntent
    from tests.conftest import MockProcessor
    from tests.test_durable_dispatch import make_flow

    class Unavailable(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args, **kwargs):
            raise OSError("result storage unavailable")

        async def record_operation_failure(self, *args):
            raise OSError("recovery storage unavailable")

    class CallbackFirst(MockProcessor):
        operation_capabilities = {OperationType.CHARGE: OperationCapabilities()}

        @classmethod
        async def submit_operation(cls, operation, *, config):
            await repository.apply_observation(
                "pay",
                PaymentObservation(
                    operation_id=operation.operation_id,
                    outcome=OperationOutcome(
                        OperationState.SUCCEEDED, settled_amount=Decimal("20")
                    ),
                ),
            )
            return OperationOutcome(
                OperationState.SUCCEEDED, settled_amount=Decimal("30")
            )

    from getpaid_core.durable import PaymentFacts

    repository = Unavailable(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=CallbackFirst.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, flow = make_flow(
        CallbackFirst,
        repository=repository,
        restricted_operations=frozenset({OperationType.CHARGE}),
    )
    with pytest.raises(OperationPersistenceError):
        await flow.execute_operation(
            "pay",
            OperationIntent("intent", OperationType.CHARGE, Decimal("30")),
            now=NOW,
        )
    discovered = await repository.list_unresolved_operations()
    assert [record.operation_id for record in discovered] == ["intent"]
    assert discovered[0].state is OperationState.SUCCEEDED
    assert (
        await repository.get_payment_facts("pay")
    ).captured_funds == Decimal("20")


async def test_query_cannot_acknowledge_paused_submission_response():
    from getpaid_core.durable import LookupSemantics
    from getpaid_core.durable import OperationCapabilities
    from getpaid_core.durable import OperationIntent
    from tests.conftest import MockProcessor
    from tests.test_durable_dispatch import make_flow

    callback_done, finish = asyncio.Event(), asyncio.Event()

    class FailingAfterQuery(InMemoryDurableRepository):
        fail = False

        async def record_operation_outcome(self, *args, **kwargs):
            if self.fail:
                raise OSError("result storage unavailable")
            return await super().record_operation_outcome(*args, **kwargs)

        async def record_operation_failure(self, *args):
            raise OSError("recovery storage unavailable")

    class Queryable(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                lookup_semantics=LookupSemantics.AUTHORITATIVE,
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            await repository.apply_observation(
                "pay",
                PaymentObservation(
                    operation_id="intent",
                    outcome=OperationOutcome(
                        OperationState.SUCCEEDED, settled_amount=Decimal("20")
                    ),
                ),
            )
            callback_done.set()
            await finish.wait()
            return OperationOutcome(
                OperationState.SUCCEEDED, settled_amount=Decimal("30")
            )

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            return OperationOutcome(
                OperationState.SUCCEEDED, settled_amount=Decimal("20")
            )

    from getpaid_core.durable import PaymentFacts

    repository = FailingAfterQuery(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=Queryable.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, flow = make_flow(Queryable, repository=repository)
    task = asyncio.create_task(
        flow.execute_operation(
            "pay",
            OperationIntent("intent", OperationType.CHARGE, Decimal("30")),
            now=NOW,
        )
    )
    await callback_done.wait()
    try:
        await flow.reconcile_operation("pay", "intent", now=NOW)
        repository.fail = True
    finally:
        finish.set()
    with pytest.raises(OperationPersistenceError):
        await task
    assert [
        r.operation_id for r in await repository.list_unresolved_operations()
    ] == ["intent"]


async def test_flagged_terminal_operation_can_still_be_queried_without_resubmission():
    from getpaid_core.durable import LookupSemantics
    from getpaid_core.durable import OperationCapabilities
    from getpaid_core.durable import OperationIntent
    from tests.conftest import MockProcessor
    from tests.test_durable_dispatch import make_flow

    lookups = []

    class Queryable(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                lookup_semantics=LookupSemantics.AUTHORITATIVE,
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            return OperationOutcome(OperationState.SUCCEEDED)

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            lookups.append(operation.operation_id)
            return OperationOutcome(OperationState.SUCCEEDED)

    repository, flow = make_flow(Queryable)
    await flow.execute_operation(
        "pay", OperationIntent("intent", OperationType.CHARGE), now=NOW
    )
    await repository.record_operation_outcome(
        "pay", "intent", OperationOutcome(OperationState.REJECTED)
    )
    result = await flow.reconcile_operation(
        "pay", "intent", now=NOW, resubmit=True
    )
    assert lookups == ["intent"]
    assert result.outcome is OperationState.SUCCEEDED
    assert result.operation.submission_attempts == 1
    assert result.reconciliation_required


async def test_normalized_plugin_subclass_cannot_smuggle_extra_evidence():
    class PluginOutcome(OperationOutcome):
        raw_payload = {"authorization": "secret"}

    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE,
        PluginOutcome(OperationState.SUCCEEDED, settled_amount=Decimal("101")),
    )
    result = await flow.execute_operation("pay", intent, now=NOW)
    retained = (
        await repository.get_operation("pay", "intent")
    ).conflicting_outcomes
    assert len(retained) == 1
    assert type(retained[0]) is OperationOutcome
    assert "secret" not in repr(retained)
    assert result.snapshot.captured_funds == 0


async def test_unsafe_callback_outcome_is_rejected_before_dispute_or_replay_storage():
    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE, OperationOutcome(OperationState.UNKNOWN)
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    from getpaid_core.exceptions import InvalidTransitionError

    with pytest.raises(InvalidTransitionError):
        await repository.apply_observation(
            "pay",
            PaymentObservation(
                operation_id="intent",
                provider_event_id="event-1",
                outcome=OperationOutcome(
                    OperationState.SUCCEEDED, correlation="Bearer secret"
                ),
            ),
        )
    assert await repository.get_payment_facts("pay") == before.snapshot
    valid = await repository.apply_observation(
        "pay",
        PaymentObservation(
            operation_id="intent",
            provider_event_id="event-1",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED, correlation="capture-1"
            ),
        ),
    )
    assert valid.applied
