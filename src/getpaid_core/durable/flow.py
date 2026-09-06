"""The flow boundary over a durable repository.

Every mutation here addresses a payment by identity, is planned against
the payment's *current* durable state, and returns committed state. The
legacy observation methods accept a caller's payment object for parsing,
but none of its financial fields is ever written back. Commands accept
only a payment identity and immutable operation intent; provider routing
comes from stored facts.

Provider I/O happens outside every repository atomic boundary. Commands
first durably reserve an intent and claim the submission right; then the
processor receives the frozen operation, and the normalized outcome is
committed together with its financial effects.

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
from time import monotonic
from typing import Any

from getpaid_core.durable.evidence import RecoveryEvidence
from getpaid_core.durable.evidence import normalize_outcome
from getpaid_core.durable.evidence import safe_handle
from getpaid_core.durable.provider import LookupSemantics
from getpaid_core.durable.provider import OperationCapabilities
from getpaid_core.durable.provider import OperationNotFound
from getpaid_core.durable.provider import OperationResult
from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.repository import DurablePaymentRepository
from getpaid_core.durable.repository import require_durable_state
from getpaid_core.durable.resolution import OperatorResolution
from getpaid_core.exceptions import CommunicationError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.exceptions import OperationEvidenceError
from getpaid_core.exceptions import OperationPersistenceError
from getpaid_core.exceptions import UnsupportedProcessorError
from getpaid_core.flow import BaseFlow
from getpaid_core.flow import OperationValidator
from getpaid_core.processor import BaseProcessor
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
        recovery_timeout: float = 5.0,
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
        if not 0 < recovery_timeout < float("inf"):
            raise ValueError("recovery_timeout must be positive and finite.")
        self.recovery_timeout = recovery_timeout

    def _capability(
        self, processor: Any, operation_type: OperationType
    ) -> OperationCapabilities:
        capabilities = processor.operation_capabilities
        capability = (
            capabilities.get(operation_type)
            if isinstance(capabilities, Mapping)
            else None
        )
        if not isinstance(capability, OperationCapabilities):
            raise UnsupportedProcessorError(
                f"Processor {processor.slug!r} must declare durable operation "
                "capabilities before submission."
            )
        methods = ["submit_operation"]
        if capability.lookup_semantics is not LookupSemantics.UNSUPPORTED:
            methods.append("lookup_operation")
        for name in methods:
            method = getattr(processor, name, None)
            default = getattr(BaseProcessor, name)
            if not callable(method) or getattr(
                method, "__func__", method
            ) is getattr(default, "__func__", default):
                raise UnsupportedProcessorError(
                    f"Durable capability requires {name} implementation."
                )
        if (
            capability.idempotency_window is None
            and capability.lookup_semantics.value == "unsupported"
            and operation_type not in self.restricted_operations
        ):
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
        started = monotonic()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        facts = await self.repository.get_payment_facts(payment_id)
        context = self._run_operation_validators(
            operation=intent.operation_type.value, payment=facts, intent=intent
        )
        intent = context["intent"]
        existing = await self.repository.get_operation(
            payment_id, intent.operation_id
        )
        operation = None
        if existing is not None:
            # Validate same-ID semantics atomically, even for terminal reads.
            operation = await self.repository.reserve_operation(
                payment_id, intent
            )
            if operation.state is not OperationState.RESERVED:
                return await self._operation_result(operation)
        processor = self.registry.get_by_slug(facts.backend)
        capability = self._capability(processor, intent.operation_type)
        if operation is None:
            operation = await self.repository.reserve_operation(
                payment_id, intent
            )
        if operation.state is not OperationState.RESERVED:
            return await self._operation_result(operation)
        deadline = (
            now + capability.idempotency_window
            if capability.idempotency_window is not None
            else None
        )
        claim = await self.repository.claim_submission(
            payment_id,
            operation.operation_id,
            expected_attempt=operation.submission_attempts,
            now=now,
            retry_until=deadline,
            idempotency_scope=capability.idempotency_scope,
        )
        if not claim.granted:
            return await self._operation_result(claim.operation)
        return await self._submit(
            processor, claim.operation, capability, now=now, started=started
        )

    async def _submit(
        self,
        processor: Any,
        operation: OperationRecord,
        capability: OperationCapabilities,
        *,
        now: datetime,
        started: float,
    ) -> OperationResult:
        # Claim acknowledgement may have consumed the entire key lifetime.
        # Recheck after every local await, immediately before provider I/O.
        current_time = now + timedelta(seconds=monotonic() - started)
        if (
            operation.retry_until is not None
            and not self._within_submission_window(
                operation, capability, current_time
            )
        ):
            return await self._operation_result(operation)
        try:
            async with asyncio.timeout(self.provider_timeout):
                outcome = await processor.submit_operation(
                    operation, config=self.config.get(operation.backend, {})
                )
        except (TimeoutError, CommunicationError):
            outcome = OperationOutcome(OperationState.UNKNOWN)
        return await self._record_evidence(operation, outcome)

    async def _record_evidence(
        self,
        operation: OperationRecord,
        outcome: object,
        *,
        submission_response: bool = True,
    ) -> OperationResult:
        evidence = RecoveryEvidence.from_outcome(outcome)
        context: dict[str, object] = {
            "payment_id": operation.payment_id,
            "operation_id": operation.operation_id,
            "operation_type": operation.operation_type.value,
            "correlation": evidence.correlation
            or safe_handle(operation.correlation),
            "evidence": evidence,
        }
        try:
            if not isinstance(outcome, OperationOutcome):
                raise InvalidTransitionError(
                    "Processor must return a normalized OperationOutcome."
                )
            outcome = normalize_outcome(outcome)
            plan = await self.repository.record_operation_outcome(
                operation.payment_id,
                operation.operation_id,
                outcome,
                response_attempt=(
                    operation.submission_attempts
                    if submission_response
                    else None
                ),
            )
        except (InvalidTransitionError, OperationConflictError) as exc:
            context["recovery_recorded"] = await self._retain_failure(
                operation, evidence
            )
            raise OperationEvidenceError(
                "Provider evidence could not be applied; "
                "reconcile the durable intent.",
                context=context,
            ) from exc
        except Exception as exc:
            context["recovery_recorded"] = await self._retain_failure(
                operation, evidence
            )
            raise OperationPersistenceError(
                "Operation evidence could not be committed; reconcile the "
                "durable intent before any further submission.",
                context=context,
            ) from exc
        return OperationResult(plan.operation, plan.facts)

    async def _retain_failure(
        self, operation: OperationRecord, evidence: RecoveryEvidence
    ) -> bool:
        """Best effort, bounded local retention; the original error wins.

        Secondary storage errors are deliberately represented by False, not
        logged with potentially sensitive messages. No task is detached or
        shielded here. Cancellation cleanup is a separate contract.
        """
        try:
            async with asyncio.timeout(self.recovery_timeout):
                await self.repository.record_operation_failure(
                    operation.payment_id, operation.operation_id, evidence
                )
        except Exception:
            return False
        return True

    async def resolve_operation(
        self,
        payment_id: str,
        operation_id: str,
        resolution: OperatorResolution,
        *,
        expected_operation: OperationRecord,
        expected_facts: PaymentFacts,
    ) -> OperationResult:
        """Commit an authorized operator decision; never contact the provider.

        The application supplies the reviewed snapshots and enforces access
        control. Stale decisions conflict; local commit failures propagate
        without inventing a committed result. Retry the same resolution ID.
        """
        plan = await self.repository.resolve_operation(
            payment_id,
            operation_id,
            resolution,
            expected_operation=expected_operation,
            expected_facts=expected_facts,
        )
        return OperationResult(plan.operation, plan.facts)

    async def reconcile_operation(
        self,
        payment_id: str,
        operation_id: str,
        *,
        now: datetime,
        resubmit: bool = False,
    ) -> OperationResult:
        """Reconcile first, then optionally claim one safe submission retry.

        A caller explicitly requests resubmission. It is refused without the
        original key scope and a still-valid window, including enough time for
        the bounded provider call. No lease expiry or not-found result grants
        an independent right to submit.
        """
        started = monotonic()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        operation = await self.repository.get_operation(
            payment_id, operation_id
        )
        if operation is None:
            raise OperationConflictError(
                "No reserved operation with that identity."
            )
        if (
            not operation.is_active
            and not operation.reconciliation_required
            and not operation.response_pending
        ) or operation.state is OperationState.RESERVED:
            return await self._operation_result(operation)
        processor = self.registry.get_by_slug(operation.backend)
        capability = self._capability(processor, operation.operation_type)
        expected_attempt = operation.submission_attempts
        if capability.lookup_semantics is not LookupSemantics.UNSUPPORTED:
            try:
                async with asyncio.timeout(self.provider_timeout):
                    outcome = await processor.lookup_operation(
                        operation, config=self.config.get(operation.backend, {})
                    )
            except (TimeoutError, CommunicationError):
                # A failed query cannot erase previously established acceptance.
                return await self._operation_result(operation)
            if type(outcome) is OperationNotFound:
                outcome = OperationOutcome(
                    OperationState.REJECTED
                    if capability.lookup_semantics
                    is LookupSemantics.AUTHORITATIVE_INCLUDING_ABSENCE
                    else OperationState.UNKNOWN
                )
            result = await self._record_evidence(
                operation, outcome, submission_response=False
            )
            operation = result.operation
        result = await self._operation_result(operation)
        current_time = now + timedelta(seconds=monotonic() - started)
        if not resubmit or not self._retry_is_safe(
            result, capability, current_time
        ):
            return result
        claim = await self.repository.claim_submission(
            payment_id,
            operation_id,
            expected_attempt=expected_attempt,
            now=current_time,
        )
        if not claim.granted:
            return await self._operation_result(claim.operation)
        return await self._submit(
            processor, claim.operation, capability, now=now, started=started
        )

    def _retry_is_safe(
        self,
        result: OperationResult,
        capability: OperationCapabilities,
        now: datetime,
    ) -> bool:
        operation = result.operation
        if result.reconciliation_required or operation.state not in {
            OperationState.UNKNOWN,
            OperationState.SUBMITTING,
        }:
            return False
        return self._within_submission_window(operation, capability, now)

    def _within_submission_window(
        self,
        operation: OperationRecord,
        capability: OperationCapabilities,
        now: datetime,
    ) -> bool:
        if (
            operation.submitted_at is None
            or operation.retry_until is None
            or capability.idempotency_window is None
            or capability.idempotency_scope != operation.idempotency_scope
        ):
            return False
        deadline = min(
            operation.retry_until,
            operation.submitted_at + capability.idempotency_window,
        )
        return (
            operation.submitted_at <= now
            and now + timedelta(seconds=self.provider_timeout) < deadline
        )

    async def _operation_result(
        self, operation: OperationRecord
    ) -> OperationResult:
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
