"""Trusted replay records and provider-metadata ownership.

These cover the failure the finding reproduces: provider metadata sharing
write ownership with core's replay bookkeeping, so a plugin's payload
could erase committed history, prepopulate identities for events never
applied, and thereby suppress a genuine financial change.
"""

import asyncio
from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import ReplayRecord
from getpaid_core.durable import plan_observation
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.types import PaymentUpdate


REQUIRED = Decimal("100.00")


def prepared_facts(**overrides) -> PaymentFacts:
    return PaymentFacts(
        payment_id=overrides.pop("payment_id", "pay-1"),
        backend=overrides.pop("backend", "mock"),
        amount_required=REQUIRED,
        status=PaymentStatus.PREPARED,
        **overrides,
    )


def capture(amount: str, event_identity: str | None = None, **overrides):
    return PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal(amount),
        provider_event_id=event_identity,
        **overrides,
    )


async def repository_with(facts: PaymentFacts) -> InMemoryDurableRepository:
    return InMemoryDurableRepository([facts])


# --- Trusted history lives outside provider metadata -----------------


async def test_forged_metadata_cannot_suppress_a_genuine_capture():
    """The finding's own reproduction, against the durable contract."""
    repository = await repository_with(prepared_facts())

    await repository.apply_observation(
        "pay-1",
        capture(
            "40.00", "first", provider_data={"applied_event_ids": ["future"]}
        ),
    )
    genuine = await repository.apply_observation(
        "pay-1", capture("100.00", "future")
    )

    assert genuine.applied is True
    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == REQUIRED


async def test_metadata_cannot_erase_committed_replay_history():
    repository = await repository_with(prepared_facts())
    await repository.apply_observation("pay-1", capture("40.00", "e-1"))

    await repository.apply_observation(
        "pay-1",
        capture("40.00", "e-2", provider_data={"applied_event_ids": []}),
    )
    replayed = await repository.apply_observation(
        "pay-1", capture("40.00", "e-1")
    )

    assert replayed.applied is False


async def test_creation_metadata_cannot_seed_trusted_history():
    """A payment created carrying lookalike keys has no replay evidence."""
    repository = await repository_with(
        prepared_facts(provider_data={"applied_event_ids": ["seeded"]})
    )

    plan = await repository.apply_observation(
        "pay-1", capture("100.00", "seeded")
    )

    assert plan.applied is True
    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == REQUIRED


async def test_legacy_lookalike_keys_stay_readable_provider_metadata():
    """They are preserved, and never consulted as bookkeeping."""
    repository = await repository_with(
        prepared_facts(provider_data={"applied_event_ids": ["seeded"]})
    )

    facts = await repository.get_payment_facts("pay-1")

    assert facts.provider_data["applied_event_ids"] == ["seeded"]


async def test_same_length_external_list_edit_changes_nothing():
    """No caller-owned mutable event list is authoritative."""
    metadata = {"applied_event_ids": ["e-1"]}
    repository = await repository_with(prepared_facts())
    await repository.apply_observation(
        "pay-1", capture("40.00", "e-1", provider_data=metadata)
    )

    metadata["applied_event_ids"][0] = "e-2"
    replayed = await repository.apply_observation(
        "pay-1", capture("40.00", "e-1")
    )
    fresh = await repository.apply_observation(
        "pay-1", capture("100.00", "e-2")
    )

    assert replayed.applied is False
    assert fresh.applied is True


async def test_committed_metadata_is_not_the_callers_mapping():
    metadata = {"reference": "r-1"}
    repository = await repository_with(prepared_facts())

    await repository.apply_observation(
        "pay-1", capture("40.00", "e-1", provider_data=metadata)
    )
    metadata["reference"] = "tampered"

    facts = await repository.get_payment_facts("pay-1")
    assert facts.provider_data["reference"] == "r-1"
    with pytest.raises(TypeError):
        facts.provider_data["reference"] = "tampered"


# --- Event identity scope and content comparison ---------------------


async def test_genuine_duplicate_delivery_is_idempotent():
    repository = await repository_with(prepared_facts())

    first = await repository.apply_observation(
        "pay-1", capture("100.00", "e-1")
    )
    second = await repository.apply_observation(
        "pay-1", capture("100.00", "e-1")
    )

    assert (first.applied, second.applied) == (True, False)
    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == REQUIRED
    assert facts.reconciliation_required is False


async def test_duplicate_delivery_survives_transport_noise():
    """Only core-owned semantic content decides that two events match."""
    repository = await repository_with(prepared_facts())

    await repository.apply_observation(
        "pay-1", capture("100.00", "e-1", provider_data={"raw": "first"})
    )
    replayed = await repository.apply_observation(
        "pay-1", capture("100", "e-1", provider_data={"raw": "retransmit"})
    )

    assert replayed.applied is False
    assert (await repository.get_payment_facts("pay-1")).captured_funds == (
        REQUIRED
    )


async def test_conflicting_reuse_is_detected_not_silently_suppressed():
    repository = await repository_with(prepared_facts())
    await repository.apply_observation("pay-1", capture("40.00", "e-1"))

    plan = await repository.apply_observation("pay-1", capture("100.00", "e-1"))

    assert plan.applied is False
    assert plan.facts.reconciliation_required is True
    flagged = await repository.list_payments_requiring_reconciliation()
    assert [facts.payment_id for facts in flagged] == ["pay-1"]


def test_event_identity_is_scoped_to_its_provider_and_payment():
    facts = prepared_facts()
    update = capture("40.00", "e-1")

    record = ReplayRecord.for_observation(facts, update)
    other_backend = ReplayRecord.for_observation(
        prepared_facts(backend="other"), update
    )
    other_payment = ReplayRecord.for_observation(
        prepared_facts(payment_id="pay-2"), update
    )

    assert record.scoped_identity == ("pay-1", "mock", "e-1")
    assert record.scoped_identity != other_backend.scoped_identity
    assert record.scoped_identity != other_payment.scoped_identity


def test_evidence_from_another_provider_does_not_suppress_an_observation():
    facts = prepared_facts()
    foreign = ReplayRecord.for_observation(
        prepared_facts(backend="other"), capture("40.00", "e-1")
    )

    plan = plan_observation(facts, [foreign], capture("40.00", "e-1"))

    assert plan.applied is True
    assert plan.facts.captured_funds == Decimal("40.00")


# --- Malformed metadata is rejected atomically -----------------------


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({1: "numeric"}, id="numeric-key"),
        pytest.param({("a", "b"): "tuple"}, id="tuple-key"),
    ],
)
async def test_malformed_metadata_keeps_funds_and_history(metadata):
    repository = await repository_with(prepared_facts())
    await repository.apply_observation("pay-1", capture("40.00", "e-1"))

    with pytest.raises(InvalidTransitionError):
        await repository.apply_observation(
            "pay-1", capture("100.00", "e-2", provider_data=metadata)
        )

    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == Decimal("40.00")
    replayed = await repository.apply_observation(
        "pay-1", capture("40.00", "e-1")
    )
    assert replayed.applied is False


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(["future"], id="list"),
        pytest.param(7, id="integer"),
    ],
)
async def test_malformed_event_identity_is_rejected(identity):
    repository = await repository_with(prepared_facts())

    with pytest.raises(InvalidTransitionError):
        await repository.apply_observation("pay-1", capture("100.00", identity))

    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == Decimal("0")


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({1: "numeric"}, id="numeric-key"),
        pytest.param(["applied_event_ids"], id="not-a-mapping"),
    ],
)
def test_malformed_creation_metadata_is_refused(metadata):
    with pytest.raises(InvalidTransitionError):
        PaymentFacts(
            payment_id="pay-1",
            amount_required=REQUIRED,
            provider_data=metadata,
        )


async def test_failed_atomic_application_leaves_state_untouched():
    """An impossible transition commits neither money nor evidence."""
    repository = await repository_with(prepared_facts())
    await repository.apply_observation("pay-1", capture("40.00", "e-1"))

    with pytest.raises(InvalidTransitionError):
        await repository.apply_observation(
            "pay-1",
            PaymentUpdate(
                payment_event=PaymentEvent.FAILED,
                paid_amount=Decimal("80.00"),
                provider_event_id="e-2",
            ),
        )

    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == Decimal("40.00")
    assert facts.reconciliation_required is False
    replayed = await repository.apply_observation(
        "pay-1", capture("40.00", "e-1")
    )
    assert replayed.applied is False
    rejected = await repository.apply_observation(
        "pay-1", capture("100.00", "e-2")
    )
    assert rejected.applied is True


# --- Detached snapshots ----------------------------------------------


async def test_detached_forged_snapshots_cannot_falsify_history():
    """Independent callers, each holding their own detached facts."""
    repository = await repository_with(prepared_facts())
    # Facts are immutable, so a read taken before the race *is* a
    # detached snapshot: nothing either caller does can reach it.
    detached = await repository.get_payment_facts("pay-1")

    await asyncio.gather(
        repository.apply_observation(
            "pay-1",
            capture(
                "40.00",
                "first",
                provider_data={"applied_event_ids": ["future"]},
            ),
        ),
        repository.apply_observation("pay-1", capture("100.00", "future")),
    )

    facts = await repository.get_payment_facts("pay-1")
    assert detached.captured_funds == Decimal("0")
    assert facts.captured_funds == REQUIRED
    for identity in ("first", "future"):
        replayed = await repository.apply_observation(
            "pay-1", capture(str(facts.captured_funds), identity)
        )
        assert replayed.applied is False, identity
