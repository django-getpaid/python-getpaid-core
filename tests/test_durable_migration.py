"""Migrating released 3.x payment records into the durable contract."""

from dataclasses import replace
from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import LegacyPaymentState
from getpaid_core.durable import MigrationFinding
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationType
from getpaid_core.durable import plan_migration
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import ReconciliationBlockedError
from getpaid_core.types import PaymentUpdate
from tests.conftest import MockPayment


REQUIRED = Decimal("100.00")


def legacy(**overrides) -> LegacyPaymentState:
    return LegacyPaymentState(
        payment_id=overrides.pop("payment_id", "pay-1"),
        amount_required=overrides.pop("amount_required", REQUIRED),
        backend=overrides.pop("backend", "mock"),
        **overrides,
    )


def charge_intent(operation_id: str = "op-1") -> OperationIntent:
    return OperationIntent(
        operation_id=operation_id, operation_type=OperationType.CHARGE
    )


# --- What migration preserves ----------------------------------------


def test_legacy_amounts_and_identity_are_preserved():
    plan = plan_migration(
        legacy(
            amount_paid=Decimal("40.00"),
            amount_locked=Decimal("60.00"),
            amount_refunded=Decimal("10.00"),
            status=PaymentStatus.PARTIALLY_REFUNDED,
            external_id="ext-1",
            fraud_status=FraudStatus.ACCEPTED,
            fraud_message="cleared",
        )
    )

    facts = plan.facts
    assert facts.payment_id == "pay-1"
    assert facts.backend == "mock"
    assert facts.amount_required == REQUIRED
    assert facts.captured_funds == Decimal("40.00")
    assert facts.remaining_authorization == Decimal("60.00")
    assert facts.refunded_funds == Decimal("10.00")
    assert facts.status == PaymentStatus.PARTIALLY_REFUNDED
    assert facts.external_id == "ext-1"
    assert facts.fraud_status == FraudStatus.ACCEPTED
    assert facts.fraud_message == "cleared"


def test_legacy_metadata_is_preserved_including_lookalike_keys():
    plan = plan_migration(
        legacy(
            provider_data={
                "applied_event_ids": ["e-1", "e-2"],
                "refund_reference": "r-1",
            }
        )
    )

    assert plan.facts.provider_data["refund_reference"] == "r-1"
    assert plan.facts.provider_data["applied_event_ids"] == ["e-1", "e-2"]


def test_a_legacy_payment_object_migrates_without_being_written_to():
    payment = MockPayment(
        status=PaymentStatus.PREPARED, provider_data={"reference": "r-1"}
    )

    plan = plan_migration(LegacyPaymentState.from_payment(payment))

    assert plan.facts.payment_id == payment.id
    assert plan.facts.backend == payment.backend
    assert payment.provider_data == {"reference": "r-1"}
    assert payment.status == PaymentStatus.PREPARED


# --- What migration refuses to invent ---------------------------------


async def test_historical_event_ids_are_not_promoted_to_trusted_evidence():
    plan = plan_migration(
        legacy(
            amount_paid=Decimal("40.00"),
            status=PaymentStatus.PARTIAL,
            provider_data={"applied_event_ids": ["e-1"]},
        )
    )
    repository = InMemoryDurableRepository([plan.facts])

    redelivered = await repository.apply_observation(
        "pay-1",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("40.00"),
            provider_event_id="e-1",
        ),
    )

    assert MigrationFinding.UNPROMOTED_EVENT_HISTORY in plan.findings
    assert redelivered.applied is True
    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == Decimal("40.00")


def test_an_unpromoted_event_history_does_not_block_mutation():
    plan = plan_migration(
        legacy(
            amount_paid=REQUIRED,
            status=PaymentStatus.PAID,
            provider_data={"applied_event_ids": ["e-1"]},
        )
    )

    assert plan.findings == (MigrationFinding.UNPROMOTED_EVENT_HISTORY,)
    assert plan.mutation_blocked is False


async def test_no_operation_id_is_invented_for_legacy_history():
    plan = plan_migration(
        legacy(amount_paid=REQUIRED, status=PaymentStatus.PAID)
    )
    repository = InMemoryDurableRepository([plan.facts])

    assert await repository.list_unresolved_operations() == ()


# --- Ambiguous and pending records are blocked ------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param(
            {"amount_refunded": Decimal("10.00")},
            "refunded more than was captured",
            id="refund-without-capture",
        ),
        pytest.param(
            {
                "amount_paid": Decimal("120.00"),
                "status": PaymentStatus.PAID,
            },
            "captured more than was required",
            id="overcapture",
        ),
        pytest.param(
            {"status": "awaiting_courier"},
            "status core does not define",
            id="unknown-status",
        ),
    ],
)
def test_ambiguous_records_migrate_blocked(overrides, reason):
    plan = plan_migration(legacy(**overrides))

    assert MigrationFinding.AMBIGUOUS_FINANCIAL_RECORD in plan.findings, reason
    assert plan.mutation_blocked is True
    assert plan.facts.reconciliation_required is True


@pytest.mark.parametrize(
    ("status", "amount_paid"),
    [
        pytest.param(PaymentStatus.IN_CHARGE, Decimal("0"), id="in-charge"),
        pytest.param(
            PaymentStatus.REFUND_STARTED, REQUIRED, id="refund-started"
        ),
    ],
)
def test_legacy_pending_operations_migrate_blocked(status, amount_paid):
    plan = plan_migration(
        legacy(
            status=status,
            amount_paid=amount_paid,
            amount_locked=REQUIRED - amount_paid,
        )
    )

    assert MigrationFinding.PENDING_OPERATION in plan.findings
    assert plan.mutation_blocked is True


def test_malformed_legacy_metadata_is_refused():
    with pytest.raises(InvalidTransitionError):
        legacy(provider_data=["applied_event_ids"])


def test_amounts_that_are_not_money_cannot_be_migrated_at_all():
    with pytest.raises(InvalidTransitionError) as excinfo:
        plan_migration(legacy(amount_paid=40.0))

    assert "amount_paid" in str(excinfo.value)


# --- Blocked means readable, not unusable -----------------------------


async def test_a_blocked_record_is_readable_and_still_takes_observations():
    plan = plan_migration(
        legacy(status=PaymentStatus.REFUND_STARTED, amount_paid=REQUIRED)
    )
    repository = InMemoryDurableRepository([plan.facts])

    facts = await repository.get_payment_facts("pay-1")
    observed = await repository.apply_observation(
        "pay-1",
        PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("40.00"),
            provider_event_id="e-1",
        ),
    )

    assert facts.captured_funds == REQUIRED
    assert observed.applied is True
    flagged = await repository.list_payments_requiring_reconciliation()
    assert [entry.payment_id for entry in flagged] == ["pay-1"]


async def test_a_blocked_record_refuses_a_new_command():
    plan = plan_migration(
        legacy(status=PaymentStatus.IN_CHARGE, amount_locked=REQUIRED)
    )
    repository = InMemoryDurableRepository([plan.facts])

    with pytest.raises(ReconciliationBlockedError) as excinfo:
        await repository.reserve_operation("pay-1", charge_intent())

    assert "pay-1" in str(excinfo.value)


async def test_an_unambiguous_record_may_proceed():
    plan = plan_migration(
        legacy(status=PaymentStatus.PRE_AUTH, amount_locked=REQUIRED)
    )
    repository = InMemoryDurableRepository([plan.facts])

    reserved = await repository.reserve_operation("pay-1", charge_intent())

    assert plan.findings == ()
    assert plan.mutation_blocked is False
    assert reserved.resolved_amount == REQUIRED


async def test_clearing_the_requirement_lets_commands_resume():
    """Reconciliation is an adapter-side write, like the migration."""
    plan = plan_migration(
        legacy(status=PaymentStatus.IN_CHARGE, amount_locked=REQUIRED)
    )
    reconciled = replace(
        plan.facts,
        status=PaymentStatus.PRE_AUTH,
        reconciliation_required=False,
    )
    repository = InMemoryDurableRepository([reconciled])

    reserved = await repository.reserve_operation("pay-1", charge_intent())

    assert reserved.resolved_amount == REQUIRED
