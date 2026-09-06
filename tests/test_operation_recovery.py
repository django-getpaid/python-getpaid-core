"""Post-provider recovery through public durable boundaries, with no real I/O."""

from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationCapabilities
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationEvidenceError
from getpaid_core.exceptions import OperationPersistenceError
from tests.conftest import MockProcessor
from tests.test_durable_dispatch import NOW
from tests.test_durable_dispatch import make_flow


async def recovery_flow(
    operation_type, outcome, repository_type=InMemoryDurableRepository
):
    calls = []

    class Recording(MockProcessor):
        operation_capabilities = dict.fromkeys(
            OperationType, OperationCapabilities()
        )

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            return outcome

    money = operation_type in {
        OperationType.START_REFUND,
        OperationType.CANCEL_REFUND,
    }
    hold = operation_type in {OperationType.CHARGE, OperationType.RELEASE_LOCK}
    repository = repository_type(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=Recording.slug,
                captured_funds=Decimal("100") if money else Decimal("0"),
                remaining_authorization=Decimal("100")
                if hold
                else Decimal("0"),
                status="paid" if money else "pre-auth" if hold else "new",
            )
        ]
    )
    parameters = {}
    if operation_type is OperationType.CANCEL_REFUND:
        await repository.reserve_operation(
            "pay", OperationIntent("refund", OperationType.START_REFUND)
        )
        # Seed a real pending target before enabling the injected write failure.
        await InMemoryDurableRepository.record_operation_outcome(
            repository,
            "pay",
            "refund",
            OperationOutcome(
                OperationState.PROVIDER_PENDING, correlation="refund-1"
            ),
        )
        parameters["target_operation_id"] = "refund"
    _, flow = make_flow(
        Recording,
        repository=repository,
        restricted_operations=frozenset(OperationType),
    )
    return (
        repository,
        flow,
        OperationIntent("intent", operation_type, parameters=parameters),
        calls,
    )


@pytest.mark.parametrize("operation_type", list(OperationType))
@pytest.mark.parametrize(
    "state", [OperationState.SUCCEEDED, OperationState.PROVIDER_PENDING]
)
@pytest.mark.parametrize(
    "failure",
    [
        OSError("storage unavailable"),
        InvalidTransitionError("local FSM failure"),
    ],
)
async def test_post_provider_failure_retains_normalized_evidence_and_intent(
    operation_type, state, failure
):
    class FailedWrite(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args):
            raise failure

    outcome = OperationOutcome(state, correlation="provider-1")
    repository, flow, intent, calls = await recovery_flow(
        operation_type, outcome, FailedWrite
    )
    error = (
        OperationPersistenceError
        if isinstance(failure, OSError)
        else OperationEvidenceError
    )
    with pytest.raises(error) as caught:
        await flow.execute_operation("pay", intent, now=NOW)
    assert caught.value.__cause__ is failure
    assert caught.value.provider_resubmission_allowed is False
    evidence = caught.value.context["evidence"]
    assert evidence.state is state
    assert evidence.correlation == "provider-1"
    assert caught.value.context["payment_id"] == "pay"
    assert caught.value.context["operation_id"] == "intent"
    assert caught.value.context["operation_type"] == operation_type.value
    retained = await repository.get_operation("pay", "intent")
    assert retained.recovery_evidence == (evidence,)
    assert retained.reconciliation_required
    assert any(
        record.operation_id == "intent"
        for record in await repository.list_unresolved_operations()
    )
    await flow.execute_operation("pay", intent, now=NOW)
    assert len(calls) == 1
