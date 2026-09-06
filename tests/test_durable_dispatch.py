"""Provider-call evidence for durable intent dispatch; no real gateway I/O."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from getpaid_core import PluginRegistry
from getpaid_core.durable import DurablePaymentFlow, InMemoryDurableRepository
from getpaid_core.durable import OperationIntent, OperationOutcome, OperationState
from getpaid_core.durable import OperationType, PaymentFacts
from getpaid_core.exceptions import GetPaidException
from tests.conftest import MockProcessor


NOW = datetime(2026, 9, 6, tzinfo=UTC)


async def test_legacy_processor_is_refused_before_reservation_or_submission():
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(MockProcessor)
    repository = InMemoryDurableRepository([
        PaymentFacts("pay", Decimal("100"), backend=MockProcessor.slug,
                     remaining_authorization=Decimal("100"), status="pre-auth")
    ])
    flow = DurablePaymentFlow(repository, registry=registry)
    with pytest.raises(GetPaidException, match="durable.*capabilit"):
        await flow.execute_operation(
            "pay", OperationIntent("capture", OperationType.CHARGE), now=NOW
        )
    assert await repository.get_operation("pay", "capture") is None
