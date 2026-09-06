"""Durable submission ownership, immutable requests, and outcome ordering."""

from dataclasses import replace
from decimal import Decimal

import pytest

from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.rules import plan_reservation
from getpaid_core.enums import PaymentStatus


def authorized_facts(**overrides):
    return replace(
        PaymentFacts(
            payment_id="payment-1",
            backend="provider",
            amount_required=Decimal("100"),
            remaining_authorization=Decimal("100"),
            status=PaymentStatus.PRE_AUTH,
        ),
        **overrides,
    )


def charge_intent(**overrides):
    return OperationIntent(
        **{
            "operation_id": "charge-1",
            "operation_type": OperationType.CHARGE,
            **overrides,
        }
    )


def test_reservation_owns_nested_parameters_and_normalizes_mapping_order():
    source = {"items": [{"price": Decimal("1.00"), "name": "a"}]}
    intent = charge_intent(parameters=source)
    record = plan_reservation(authorized_facts(), (), intent).operation
    source["items"][0]["price"] = Decimal("9")
    source["items"].append({"name": "b"})

    same = charge_intent(
        parameters={"items": [{"name": "a", "price": Decimal("1")}]}
    )
    assert intent.parameters_digest == same.parameters_digest
    assert record.parameters["items"][0]["price"] == Decimal("1")
    assert len(record.parameters["items"]) == 1
    with pytest.raises(TypeError):
        record.parameters["items"][0]["price"] = Decimal("4")
    assert record.starting_authorization == Decimal("100")
    assert record.backend == "provider"
    assert record.idempotency_key
    assert (
        plan_reservation(authorized_facts(), (record,), same).operation
        == record
    )
