"""A reference durable repository, in memory.

This exists so the contract has a runnable shape: the conformance suite
checks itself against it, and adapter authors can read one complete
implementation of the semantic operations.

It is **not** production storage and proves nothing about a real adapter.
Its atomic boundary is an in-process lock, which ADR 0001 rejects for
real deployments precisely because it does not hold across workers. A
framework wrapper must reach the same semantics with a transaction, a row
lock or a compare-and-set retry against its own database.
"""

import asyncio
from collections.abc import Iterable
from collections.abc import Sequence
from datetime import datetime

from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import ReplayRecord
from getpaid_core.durable.records import SubmissionPlan
from getpaid_core.durable.rules import plan_observation
from getpaid_core.durable.rules import plan_outcome
from getpaid_core.durable.rules import plan_reservation
from getpaid_core.durable.rules import plan_submission
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.types import PaymentUpdate


class InMemoryDurableRepository:
    """Reference implementation of the durable repository contract."""

    def __init__(self, facts: Iterable[PaymentFacts] = ()) -> None:
        self._facts: dict[str, PaymentFacts] = {
            entry.payment_id: entry for entry in facts
        }
        self._operations: dict[str, list[OperationRecord]] = {}
        self._replay: dict[str, list[ReplayRecord]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, payment_id: str) -> asyncio.Lock:
        return self._locks.setdefault(payment_id, asyncio.Lock())

    async def get_payment_facts(self, payment_id: str) -> PaymentFacts:
        return self._facts[payment_id]

    async def apply_observation(
        self, payment_id: str, update: PaymentUpdate | None
    ) -> ObservationPlan:
        async with self._lock_for(payment_id):
            plan = plan_observation(
                self._facts[payment_id],
                self._replay.setdefault(payment_id, []),
                update,
            )
            self._facts[payment_id] = plan.facts
            if plan.replay_record is not None:
                self._replay[payment_id].append(plan.replay_record)
            return plan

    async def reserve_operation(
        self, payment_id: str, intent: OperationIntent
    ) -> OperationRecord:
        async with self._lock_for(payment_id):
            operations = self._operations.setdefault(payment_id, [])
            plan = plan_reservation(self._facts[payment_id], operations, intent)
            if plan.created:
                operations.append(plan.operation)
            return plan.operation

    async def claim_submission(
        self,
        payment_id: str,
        operation_id: str,
        *,
        expected_attempt: int,
        now: datetime,
        retry_until: datetime | None = None,
        idempotency_scope: str | None = None,
    ) -> SubmissionPlan:
        """Claim locally; the caller reconciles before asking for a retry."""
        async with self._lock_for(payment_id):
            operations = self._operations.get(payment_id, [])
            index = self._operation_index(payment_id, operation_id, operations)
            plan = plan_submission(
                self._facts[payment_id],
                operations[index],
                expected_attempt=expected_attempt,
                now=now,
                retry_until=retry_until,
                idempotency_scope=idempotency_scope,
            )
            operations[index] = plan.operation
            return plan

    @staticmethod
    def _operation_index(
        payment_id: str,
        operation_id: str,
        operations: Sequence[OperationRecord],
    ) -> int:
        for index, operation in enumerate(operations):
            if operation.operation_id == operation_id:
                return index
        raise OperationConflictError(
            f"Operation {operation_id!r} was never reserved on "
            f"payment {payment_id!r}.",
            context={"payment_id": payment_id, "operation_id": operation_id},
        )

    async def record_operation_outcome(
        self, payment_id: str, operation_id: str, outcome: OperationOutcome
    ) -> OutcomePlan:
        async with self._lock_for(payment_id):
            operations = self._operations.setdefault(payment_id, [])
            index = self._operation_index(payment_id, operation_id, operations)
            plan = plan_outcome(
                self._facts[payment_id],
                operations[index],
                outcome,
                operations=operations,
            )
            replacements = {
                record.operation_id: record
                for record in (plan.operation, *plan.related_operations)
            }
            self._operations[payment_id] = [
                replacements.get(record.operation_id, record)
                for record in operations
            ]
            self._facts[payment_id] = plan.facts
            return plan

    async def get_operation(
        self, payment_id: str, operation_id: str
    ) -> OperationRecord | None:
        return next(
            (
                record
                for record in self._operations.get(payment_id, ())
                if record.operation_id == operation_id
            ),
            None,
        )

    async def list_unresolved_operations(self) -> Sequence[OperationRecord]:
        return tuple(
            record
            for records in self._operations.values()
            for record in records
            if record.is_active or record.reconciliation_required
        )

    async def list_payments_requiring_reconciliation(
        self,
    ) -> Sequence[PaymentFacts]:
        return tuple(
            facts
            for facts in self._facts.values()
            if facts.reconciliation_required
        )
