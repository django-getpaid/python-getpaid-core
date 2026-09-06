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

import asyncio
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from typing import Any

from getpaid_core.durable.provider import OperationCapabilities
from getpaid_core.durable.provider import OperationResult

from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.repository import DurablePaymentRepository
from getpaid_core.durable.repository import require_durable_state
from getpaid_core.exceptions import UnsupportedProcessorError
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
        *,
        restricted_operations: frozenset[OperationType] = frozenset(),
        provider_timeout: float = 30.0,
    ) -> None:
        super().__init__(
            require_durable_state(repository, operation="durable payment flow"),
            config,
            validators,
            registry,
        )
        self.repository: DurablePaymentRepository = repository
        self.restricted_operations = frozenset(restricted_operations)
        if not 0 < provider_timeout < float("inf"):
            raise ValueError("provider_timeout must be positive and finite.")
        self.provider_timeout = provider_timeout

    def _capability(self, processor: Any, operation_type: OperationType) -> OperationCapabilities:
        capabilities = processor.operation_capabilities
        capability = capabilities.get(operation_type) if isinstance(capabilities, Mapping) else None
        if not isinstance(capability, OperationCapabilities):
            raise UnsupportedProcessorError(
                f"Processor {processor.slug!r} must declare durable operation "
                "capabilities before submission."
            )
        if (capability.idempotency_window is None
                and capability.lookup_semantics.value == "unsupported"
                and operation_type not in self.restricted_operations):
            raise UnsupportedProcessorError(
                "This operation requires explicit restricted mode: no safe "
                "submission retry or authoritative lookup is declared."
            )
        return capability

    async def execute_operation(
        self, payment_id: str, intent: OperationIntent, *, now: datetime
    ) -> OperationResult:
        """Reserve and submit once; duplicates return committed intent state.

        ``now`` is the application's trusted, timezone-aware current time.
        Retrying an uncertain intent does not dispatch it: call
        ``reconcile_operation`` explicitly to query evidence first.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        facts = await self.repository.get_payment_facts(payment_id)
        context = self._run_operation_validators(
            operation=intent.operation_type.value, payment=facts, intent=intent
        )
        intent = context["intent"]
        processor = self.registry.get_by_slug(facts.backend)
        capability = self._capability(processor, intent.operation_type)
        operation = await self.repository.reserve_operation(payment_id, intent)
        if operation.state is not OperationState.RESERVED:
            return await self._operation_result(operation)
        deadline = (now + capability.idempotency_window
                    if capability.idempotency_window is not None else None)
        claim = await self.repository.claim_submission(
            payment_id, operation.operation_id,
            expected_attempt=operation.submission_attempts, now=now,
            retry_until=deadline, idempotency_scope=capability.idempotency_scope,
        )
        if not claim.granted:
            return await self._operation_result(claim.operation)
        async with asyncio.timeout(self.provider_timeout):
            outcome = await processor.submit_operation(
                claim.operation, config=self.config.get(facts.backend, {})
            )
        plan = await self.repository.record_operation_outcome(
            payment_id, operation.operation_id, outcome
        )
        return OperationResult(plan.operation, plan.facts)

    async def _operation_result(self, operation: OperationRecord) -> OperationResult:
        facts = await self.repository.get_payment_facts(operation.payment_id)
        return OperationResult(operation, facts)

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
