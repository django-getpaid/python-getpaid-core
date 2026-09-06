"""Durable-state contract for money-moving operations (ADR 0001).

Core supplies framework-neutral records, a semantic repository protocol,
the validation and transition rules that run against *current* durable
state, and a reusable conformance suite. Framework wrappers supply the
storage, transactions and scheduling.
"""

from getpaid_core.durable.conformance import CONFORMANCE_CHECKS
from getpaid_core.durable.conformance import RepositoryFactory
from getpaid_core.durable.conformance import run_conformance_suite
from getpaid_core.durable.flow import DurablePaymentFlow
from getpaid_core.durable.memory import InMemoryDurableRepository
from getpaid_core.durable.migration import BLOCKING_MIGRATION_FINDINGS
from getpaid_core.durable.migration import LEGACY_PENDING_STATUSES
from getpaid_core.durable.migration import LegacyPaymentState
from getpaid_core.durable.migration import MigrationFinding
from getpaid_core.durable.migration import MigrationPlan
from getpaid_core.durable.migration import plan_migration
from getpaid_core.durable.records import ACTIVE_OPERATION_STATES
from getpaid_core.durable.records import CANCELLATION_TARGET
from getpaid_core.durable.records import LEGACY_REPLAY_METADATA_KEYS
from getpaid_core.durable.records import TERMINAL_OPERATION_STATES
from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import ReplayRecord
from getpaid_core.durable.records import ReservationPlan
from getpaid_core.durable.records import observation_digest
from getpaid_core.durable.records import validate_event_identity
from getpaid_core.durable.records import validate_provider_metadata
from getpaid_core.durable.repository import MANDATORY_OPERATIONS
from getpaid_core.durable.repository import DurablePaymentRepository
from getpaid_core.durable.repository import commit_semantic_transition
from getpaid_core.durable.repository import missing_durable_operations
from getpaid_core.durable.repository import require_durable_state
from getpaid_core.durable.repository import supports_durable_state
from getpaid_core.durable.rules import plan_observation
from getpaid_core.durable.rules import plan_outcome
from getpaid_core.durable.rules import plan_reservation


__all__ = [
    "ACTIVE_OPERATION_STATES",
    "BLOCKING_MIGRATION_FINDINGS",
    "CANCELLATION_TARGET",
    "CONFORMANCE_CHECKS",
    "LEGACY_PENDING_STATUSES",
    "LEGACY_REPLAY_METADATA_KEYS",
    "MANDATORY_OPERATIONS",
    "TERMINAL_OPERATION_STATES",
    "DurablePaymentFlow",
    "DurablePaymentRepository",
    "InMemoryDurableRepository",
    "LegacyPaymentState",
    "MigrationFinding",
    "MigrationPlan",
    "ObservationPlan",
    "OperationIntent",
    "OperationOutcome",
    "OperationRecord",
    "OperationState",
    "OperationType",
    "OutcomePlan",
    "PaymentFacts",
    "ReplayRecord",
    "RepositoryFactory",
    "ReservationPlan",
    "commit_semantic_transition",
    "missing_durable_operations",
    "observation_digest",
    "plan_migration",
    "plan_observation",
    "plan_outcome",
    "plan_reservation",
    "require_durable_state",
    "run_conformance_suite",
    "supports_durable_state",
    "validate_event_identity",
    "validate_provider_metadata",
]
