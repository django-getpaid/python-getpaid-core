"""The semantic repository contract and its capability guard."""

from collections.abc import Sequence

import pytest

from getpaid_core.durable import DurablePaymentRepository
from getpaid_core.durable import ObservationPlan
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationRecord
from getpaid_core.durable import OutcomePlan
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import commit_semantic_transition
from getpaid_core.durable import missing_durable_operations
from getpaid_core.durable import require_durable_state
from getpaid_core.durable import supports_durable_state
from getpaid_core.exceptions import StateConflictError
from getpaid_core.exceptions import UnsupportedRepositoryError
from getpaid_core.types import PaymentUpdate
from tests.conftest import MockRepository


class ConformingRepository:
    """Smallest object satisfying the durable repository contract."""

    async def get_payment_facts(self, payment_id: str) -> PaymentFacts:
        raise NotImplementedError

    async def reserve_operation(
        self, payment_id: str, intent: OperationIntent
    ) -> OperationRecord:
        raise NotImplementedError

    async def apply_observation(
        self, payment_id: str, update: PaymentUpdate | None
    ) -> ObservationPlan:
        raise NotImplementedError

    async def record_operation_outcome(
        self, payment_id: str, operation_id: str, outcome: OperationOutcome
    ) -> OutcomePlan:
        raise NotImplementedError

    async def get_operation(
        self, payment_id: str, operation_id: str
    ) -> OperationRecord | None:
        raise NotImplementedError

    async def list_unresolved_operations(self) -> Sequence[OperationRecord]:
        raise NotImplementedError


def test_legacy_repository_does_not_support_durable_state():
    assert supports_durable_state(MockRepository()) is False


def test_conforming_repository_supports_durable_state():
    repository = ConformingRepository()

    assert supports_durable_state(repository) is True
    assert isinstance(repository, DurablePaymentRepository)


def test_missing_operations_name_what_the_adapter_must_add():
    missing = missing_durable_operations(MockRepository())

    assert "reserve_operation" in missing
    assert "apply_observation" in missing
    assert "list_unresolved_operations" in missing


def test_unsupported_adapter_is_rejected_before_provider_submission():
    with pytest.raises(UnsupportedRepositoryError) as excinfo:
        require_durable_state(MockRepository(), operation="charge")

    message = str(excinfo.value)
    assert "charge" in message
    assert "reserve_operation" in message


def test_guard_returns_a_conforming_repository_unchanged():
    repository = ConformingRepository()

    assert require_durable_state(repository, operation="charge") is repository


def test_state_conflict_is_locally_retryable_but_never_resubmittable():
    conflict = StateConflictError("concurrent writer won")

    assert conflict.retry_locally is True
    assert conflict.provider_resubmission_allowed is False


async def test_conflict_retry_replans_against_fresh_state():
    attempts = []

    async def commit() -> str:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise StateConflictError("compare-and-set lost")
        return "committed"

    assert await commit_semantic_transition(commit, attempts=3) == "committed"
    assert len(attempts) == 3


async def test_conflict_retry_gives_up_rather_than_looping_forever():
    async def commit() -> str:
        raise StateConflictError("compare-and-set lost")

    with pytest.raises(StateConflictError):
        await commit_semantic_transition(commit, attempts=2)


async def test_conflict_retry_does_not_swallow_other_failures():
    async def commit() -> str:
        raise UnsupportedRepositoryError("not durable")

    with pytest.raises(UnsupportedRepositoryError):
        await commit_semantic_transition(commit)
