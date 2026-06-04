"""Performance benchmarks for getpaid-core.

Run with: pytest tests/test_benchmarks.py --benchmark-only
Or: pytest tests/test_benchmarks.py --benchmark-min-rounds=50

These benchmarks establish baseline performance for:
- FSM state transitions (apply_payment_update)
- Registry lookups (get_by_slug, get_for_currency)
- Callback handling path (full flow)

Benchmarks use MockPayment and MockRepository to isolate
the core logic from I/O and network latency.
"""

from decimal import Decimal

import pytest

from getpaid_core.enums import FraudEvent
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.fsm import apply_payment_update
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import PaymentUpdate

from .conftest import MockPayment
from .conftest import MockProcessor


# ---------------------------------------------------------------------------
# FSM benchmarks
# ---------------------------------------------------------------------------


class TestFSMBenchmarks:
    """Benchmarks for FSM state transitions."""

    @pytest.fixture
    def fresh_payment(self):
        """A NEW payment ready for transitions."""
        return MockPayment(
            status=PaymentStatus.NEW,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )

    def test_bench_fsm_prepare(self, fresh_payment, benchmark):
        """Bench: NEW → PREPARED transition."""
        update = PaymentUpdate(payment_event=PaymentEvent.PREPARED)
        benchmark(lambda: apply_payment_update(fresh_payment, update))

    def test_bench_fsm_lock(self, fresh_payment, benchmark):
        """Bench: NEW → PRE_AUTH (LOCKED event)."""
        update = PaymentUpdate(
            payment_event=PaymentEvent.LOCKED,
            locked_amount=Decimal("100.00"),
        )
        benchmark(lambda: apply_payment_update(fresh_payment, update))

    def test_bench_fsm_capture(self, benchmark):
        """Bench: PRE_AUTH → PAID (PAYMENT_CAPTURED event)."""
        payment = MockPayment(
            status=PaymentStatus.PRE_AUTH,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("100.00"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("100.00"),
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_partial_capture(self, benchmark):
        """Bench: PRE_AUTH → PARTIAL (partial capture)."""
        payment = MockPayment(
            status=PaymentStatus.PRE_AUTH,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("100.00"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("60.00"),
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_refund_requested(self, benchmark):
        """Bench: PAID → REFUND_STARTED."""
        payment = MockPayment(
            status=PaymentStatus.PAID,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.REFUND_REQUESTED,
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_refund_confirmed_full(self, benchmark):
        """Bench: REFUND_STARTED → REFUNDED (full refund)."""
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("100.00"),
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_refund_confirmed_partial(self, benchmark):
        """Bench: REFUND_STARTED → PARTIAL (partial refund)."""
        payment = MockPayment(
            status=PaymentStatus.REFUND_STARTED,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("30.00"),
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_lock_released(self, benchmark):
        """Bench: PRE_AUTH → REFUNDED (lock released)."""
        def _transition():
            payment = MockPayment(
                status=PaymentStatus.PRE_AUTH,
                amount_required=Decimal("100.00"),
                amount_paid=Decimal("0"),
                amount_locked=Decimal("100.00"),
                amount_refunded=Decimal("0"),
                fraud_status="unknown",
                fraud_message="",
            )
            update = PaymentUpdate(
                payment_event=PaymentEvent.LOCK_RELEASED,
            )
            return apply_payment_update(payment, update)

        benchmark(_transition)

    def test_bench_fsm_failed(self, benchmark):
        """Bench: PRE_AUTH → FAILED."""
        payment = MockPayment(
            status=PaymentStatus.PRE_AUTH,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("100.00"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.FAILED,
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_fraud_review(self, benchmark):
        """Bench: fraud REVIEW event."""
        payment = MockPayment(
            status=PaymentStatus.NEW,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            fraud_event=FraudEvent.REVIEW,
            fraud_message="Manual review required",
        )
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_idempotent_event(self, benchmark):
        """Bench: idempotent event (already applied)."""
        payment = MockPayment(
            status=PaymentStatus.NEW,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.PREPARED,
            provider_event_id="event-1",
        )
        apply_payment_update(payment, update)
        benchmark(lambda: apply_payment_update(payment, update))

    def test_bench_fsm_invalid_transition_silent(self, benchmark):
        """Bench: FSM silently ignores invalid transitions (idempotent design)."""
        payment = MockPayment(
            status=PaymentStatus.PAID,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            amount_locked=Decimal("0"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.LOCKED,
            locked_amount=Decimal("50.00"),
        )
        benchmark(lambda: apply_payment_update(payment, update))

        # Verify no state change
        assert payment.status is PaymentStatus.PAID
        assert payment.amount_locked == Decimal("0")

    def test_bench_fsm_valid_transition(self, benchmark):
        """Bench: valid transition (baseline for comparison)."""
        payment = MockPayment(
            status=PaymentStatus.PRE_AUTH,
            amount_required=Decimal("100.00"),
            amount_paid=Decimal("0"),
            amount_locked=Decimal("100.00"),
            amount_refunded=Decimal("0"),
            fraud_status="unknown",
            fraud_message="",
        )
        update = PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("100.00"),
            provider_data={"key": "value" * 10},
        )
        benchmark(lambda: apply_payment_update(payment, update))
        assert payment.status is PaymentStatus.PAID
        assert payment.amount_paid == Decimal("100.00")


# ---------------------------------------------------------------------------
# Registry benchmarks
# ---------------------------------------------------------------------------


class TestRegistryBenchmarks:
    """Benchmarks for PluginRegistry operations."""

    @pytest.fixture
    def populated_registry(self):
        """A registry with multiple backends registered."""
        reg = PluginRegistry()
        reg._discovered = True
        # Register multiple processors to simulate real-world usage
        for i in range(10):
            backend = type(
                f"Backend{i}",
                (MockProcessor,),
                {
                    "slug": f"backend-{i}",
                    "display_name": f"Backend {i}",
                    "accepted_currencies": ("PLN", "EUR", "USD"),
                },
            )
            reg.register(backend)
        return reg

    def test_bench_registry_get_by_slug(self, populated_registry, benchmark):
        """Bench: O(1) dict lookup by slug."""
        benchmark(lambda: populated_registry.get_by_slug("backend-5"))

    def test_bench_registry_get_for_currency(self, populated_registry, benchmark):
        """Bench: filter backends by currency."""
        benchmark(lambda: populated_registry.get_for_currency("PLN"))

    def test_bench_registry_get_choices(self, populated_registry, benchmark):
        """Bench: build (slug, name) choices list."""
        benchmark(lambda: populated_registry.get_choices("EUR"))

    def test_bench_registry_get_all_currencies(self, populated_registry, benchmark):
        """Bench: collect all supported currencies."""
        benchmark(lambda: populated_registry.get_all_currencies())

    def test_bench_registry_discover(self, benchmark):
        """Bench: initial discovery (entry_points scan)."""
        reg = PluginRegistry()
        benchmark(reg.discover)

    def test_bench_registry_double_checked_lock(self, benchmark):
        """Bench: thread-safe double-checked locking."""
        reg = PluginRegistry()
        reg._discovered = False
        benchmark(reg._ensure_discovered)


# ---------------------------------------------------------------------------
# Callback handling path benchmarks
# ---------------------------------------------------------------------------


class TestCallbackBenchmarks:
    """Benchmarks for the full callback handling path.

    All PaymentFlow methods are async, so we use asyncio.run()
    inside the benchmarked function to ensure proper execution.
    """

    @pytest.fixture
    def flow_with_registry(self, mock_registry, mock_repo):
        """A PaymentFlow with mock registry and repository."""
        from getpaid_core.flow import PaymentFlow

        return PaymentFlow(
            repository=mock_repo,
            registry=mock_registry,
        )

    def test_bench_flow_create_payment(self, flow_with_registry, benchmark):
        """Bench: create_payment flow (registry lookup + repo create)."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()

        def _run():
            return asyncio.run(
                flow_with_registry.create_payment(
                    order=order,
                    backend_slug="mock",
                )
            )

        benchmark(_run)

    def test_bench_flow_prepare(self, flow_with_registry, benchmark):
        """Bench: prepare flow (validators + processor + FSM + save)."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()
        payment = asyncio.run(
            flow_with_registry.create_payment(
                order=order,
                backend_slug="mock",
            )
        )

        def _run():
            return asyncio.run(flow_with_registry.prepare(payment))

        benchmark(_run)

    def test_bench_flow_handle_callback_confirmed(self, flow_with_registry, benchmark):
        """Bench: callback handling for payment confirmed."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()
        payment = asyncio.run(
            flow_with_registry.create_payment(
                order=order,
                backend_slug="mock",
            )
        )
        asyncio.run(flow_with_registry.prepare(payment))

        def _run():
            return asyncio.run(
                flow_with_registry.handle_callback(
                    payment,
                    data={"event": "payment_confirmed", "paid_amount": "100.00", "event_id": "cb-1"},
                    headers={"Content-Type": "application/json"},
                )
            )

        benchmark(_run)

    def test_bench_flow_handle_callback_fraud(self, flow_with_registry, benchmark):
        """Bench: callback handling for fraud review."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()
        payment = asyncio.run(
            flow_with_registry.create_payment(
                order=order,
                backend_slug="mock",
            )
        )
        asyncio.run(flow_with_registry.prepare(payment))

        def _run():
            return asyncio.run(
                flow_with_registry.handle_callback(
                    payment,
                    data={"event": "fraud_review"},
                    headers={"Content-Type": "application/json"},
                )
            )

        benchmark(_run)

    def test_bench_flow_charge(self, flow_with_registry, benchmark):
        """Bench: charge flow (validators + precondition check + processor + FSM)."""
        import asyncio

        from getpaid_core.enums import PaymentStatus

        from .conftest import MockOrder

        def _run():
            order = MockOrder()
            payment = asyncio.run(
                flow_with_registry.create_payment(
                    order=order,
                    backend_slug="mock",
                )
            )
            asyncio.run(flow_with_registry.prepare(payment))
            # MockProcessor.prepare() only transitions to PREPARED.
            # charge() requires PRE_AUTH, so we set the state directly.
            payment.status = PaymentStatus.PRE_AUTH
            payment.amount_locked = Decimal("100.00")
            return asyncio.run(flow_with_registry.charge(payment))

        benchmark(_run)

    def test_bench_flow_release_lock(self, flow_with_registry, benchmark):
        """Bench: release_lock flow."""
        import asyncio

        from getpaid_core.enums import PaymentStatus

        from .conftest import MockOrder

        def _run():
            order = MockOrder()
            payment = asyncio.run(
                flow_with_registry.create_payment(
                    order=order,
                    backend_slug="mock",
                )
            )
            asyncio.run(flow_with_registry.prepare(payment))
            # MockProcessor.prepare() only transitions to PREPARED.
            # release_lock() requires PRE_AUTH, so we set the state directly.
            payment.status = PaymentStatus.PRE_AUTH
            payment.amount_locked = Decimal("100.00")
            return asyncio.run(flow_with_registry.release_lock(payment))

        benchmark(_run)

    def test_bench_flow_start_refund(self, flow_with_registry, benchmark):
        """Bench: start_refund flow."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()
        payment = asyncio.run(
            flow_with_registry.create_payment(
                order=order,
                backend_slug="mock",
            )
        )
        asyncio.run(flow_with_registry.prepare(payment))
        asyncio.run(
            flow_with_registry.handle_callback(
                payment,
                data={"event": "payment_confirmed", "paid_amount": "100.00", "event_id": "cb-1"},
                headers={"Content-Type": "application/json"},
            )
        )

        def _run():
            return asyncio.run(flow_with_registry.start_refund(payment))

        benchmark(_run)

    def test_bench_flow_fetch_and_update(self, flow_with_registry, benchmark):
        """Bench: fetch_and_update_status (PULL flow)."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()
        payment = asyncio.run(
            flow_with_registry.create_payment(
                order=order,
                backend_slug="mock",
            )
        )
        asyncio.run(flow_with_registry.prepare(payment))

        def _run():
            return asyncio.run(flow_with_registry.fetch_and_update_status(payment))

        benchmark(_run)

    def test_bench_flow_cancel_refund(self, flow_with_registry, benchmark):
        """Bench: cancel_refund flow."""
        import asyncio

        from .conftest import MockOrder

        order = MockOrder()
        payment = asyncio.run(
            flow_with_registry.create_payment(
                order=order,
                backend_slug="mock",
            )
        )
        asyncio.run(flow_with_registry.prepare(payment))
        asyncio.run(
            flow_with_registry.handle_callback(
                payment,
                data={"event": "payment_confirmed", "paid_amount": "100.00", "event_id": "cb-1"},
                headers={"Content-Type": "application/json"},
            )
        )
        asyncio.run(flow_with_registry.start_refund(payment))

        def _run():
            return asyncio.run(flow_with_registry.cancel_refund(payment))

        benchmark(_run)
