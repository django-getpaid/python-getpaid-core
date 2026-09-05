"""The flow boundary over a durable repository.

Every mutation here addresses a payment by identity, is planned against
the payment's *current* durable state, and returns committed state. The
caller's payment object is an ergonomic input -- it names the payment and
carries the backend the processor is built from -- but none of its
financial fields is ever written back.

Provider I/O happens outside the repository's atomic boundary: the
processor is called first, and only the normalized result is handed to
the repository to commit.

Choosing this flow is the cutover. It requires a repository implementing
the durable contract and refuses anything less at construction, before a
financial command can reach a provider; there is no fallback to the
released unconditional-save behaviour. ``PaymentFlow`` remains that
released path, and the two must not write the same payment state.
"""

from typing import Any

from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.repository import DurablePaymentRepository
from getpaid_core.durable.repository import require_durable_state
from getpaid_core.flow import BaseFlow
from getpaid_core.flow import OperationValidator
from getpaid_core.protocols import Payment
from getpaid_core.registry import PluginRegistry


class DurablePaymentFlow(BaseFlow):
    """Payment orchestration against durably committed state."""

    def __init__(
        self,
        repository: DurablePaymentRepository,
        config: dict[str, dict[str, Any]] | None = None,
        validators: list[OperationValidator] | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        super().__init__(
            require_durable_state(repository, operation="durable payment flow"),
            config,
            validators,
            registry,
        )
        self.repository: DurablePaymentRepository = repository

    async def handle_callback(
        self,
        payment: Payment,
        data: dict[str, Any],
        headers: dict[str, str],
        **kwargs: Any,
    ) -> ObservationPlan:
        """Verify a PUSH callback and apply it to current durable state.

        Returns the committed plan: the facts as stored after the call
        and the replay evidence committed with them.
        """
        context = self._run_operation_validators(
            operation="callback",
            payment=payment,
            data=dict(data),
            headers=dict(headers),
            kwargs=dict(kwargs),
        )
        processor = self.get_processor(payment)
        await processor.verify_callback(
            context["data"], context["headers"], **context["kwargs"]
        )
        update = await processor.handle_callback(
            context["data"], context["headers"], **context["kwargs"]
        )
        return await self.repository.apply_observation(payment.id, update)

    async def fetch_and_update_status(
        self,
        payment: Payment,
    ) -> ObservationPlan:
        """PULL flow: fetch status and apply it to current durable state.

        Polling shares the callback's atomic boundary, so it returns the
        same committed plan.
        """
        context = self._run_operation_validators(
            operation="fetch_status",
            payment=payment,
            kwargs={},
        )
        processor = self.get_processor(payment)
        update = await processor.fetch_payment_status(**context["kwargs"])
        return await self.repository.apply_observation(payment.id, update)

    async def reserve_operation(
        self,
        payment: Payment,
        intent: OperationIntent,
    ) -> OperationRecord:
        """Reserve an operation intent before submitting it to a provider.

        The reservation commits against current durable facts, so the
        amount an omitted request resolves to is frozen before any
        provider call and reused by a same-ID retry.

        Reserving is not provider acceptance and not settlement: the
        provider call happens after this returns, outside the boundary
        that committed the reservation.
        """
        context = self._run_operation_validators(
            operation="reserve_operation",
            payment=payment,
            intent=intent,
        )
        return await self.repository.reserve_operation(
            payment.id, context["intent"]
        )

    async def record_operation_outcome(
        self,
        payment: Payment,
        operation_id: str,
        outcome: OperationOutcome,
    ) -> OutcomePlan:
        """Record what a reserved operation turned out to do.

        The operation record and the financial facts it settles commit
        together. A nonterminal outcome -- ``UNKNOWN`` included -- moves
        no money and leaves the operation discoverable as unresolved
        work; it is never evidence that resubmission is safe.
        """
        context = self._run_operation_validators(
            operation="record_operation_outcome",
            payment=payment,
            operation_id=operation_id,
            outcome=outcome,
        )
        return await self.repository.record_operation_outcome(
            payment.id, context["operation_id"], context["outcome"]
        )
