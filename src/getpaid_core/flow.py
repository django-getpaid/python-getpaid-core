"""Payment flow orchestrator."""

from decimal import Decimal
from typing import Any

from getpaid_core.enums import PaymentEvent
from getpaid_core.fsm import apply_payment_update
from getpaid_core.protocols import Order
from getpaid_core.protocols import Payment
from getpaid_core.protocols import PaymentRepository
from getpaid_core.registry import PluginRegistry
from getpaid_core.registry import registry as default_registry
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult
from getpaid_core.validators import run_validators


class PaymentFlow:
    """Core payment processing orchestrator."""

    def __init__(
        self,
        repository: PaymentRepository,
        config: dict[str, dict[str, Any]] | None = None,
        validators: list | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or {}
        self.validators = validators or []
        self.registry = registry or default_registry

    async def create_payment(
        self, order: Order, backend_slug: str, **kwargs
    ) -> Payment:
        """Create a new payment for an order."""
        self.registry.get_by_slug(backend_slug)
        payment = await self.repository.create(
            order=order,
            backend=backend_slug,
            amount_required=order.get_total_amount(),
            currency=order.get_currency(),
            description=order.get_description(),
            provider_data=dict(kwargs.pop("provider_data", {})),
            **kwargs,
        )
        return payment

    async def prepare(self, payment: Payment, **kwargs) -> TransactionResult:
        """Prepare a payment for processing."""
        context = self._run_operation_validators(
            operation="prepare",
            payment=payment,
            kwargs=dict(kwargs),
        )
        processor = self.get_processor(payment)
        result = await processor.prepare_transaction(**context["kwargs"])
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.PREPARED,
                external_id=result.external_id,
                provider_data=result.provider_data,
            ),
        )
        await self.repository.save(payment)
        return result

    async def handle_callback(
        self,
        payment: Payment,
        data: dict,
        headers: dict,
        **kwargs,
    ) -> None:
        """Handle an incoming PUSH callback from the gateway."""
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
        apply_payment_update(payment, update)
        await self.repository.save(payment)

    async def fetch_and_update_status(self, payment: Payment) -> Payment:
        """PULL flow: fetch status from gateway and update."""
        context = self._run_operation_validators(
            operation="fetch_status",
            payment=payment,
            kwargs={},
        )
        processor = self.get_processor(payment)
        update = await processor.fetch_payment_status(**context["kwargs"])
        apply_payment_update(payment, update)
        await self.repository.save(payment)
        return payment

    async def charge(
        self,
        payment: Payment,
        amount: Decimal | None = None,
        **kwargs,
    ) -> ChargeResult:
        """Charge a pre-authorized payment."""
        context = self._run_operation_validators(
            operation="charge",
            payment=payment,
            kwargs={"amount": amount, **kwargs},
        )
        processor = self.get_processor(payment)
        result = await processor.charge(**context["kwargs"])
        if result.success:
            if result.async_call:
                update = PaymentUpdate(
                    payment_event=PaymentEvent.CHARGE_REQUESTED,
                    provider_data=result.provider_data,
                )
            else:
                update = PaymentUpdate(
                    payment_event=PaymentEvent.PAYMENT_CAPTURED,
                    paid_amount=payment.amount_paid + result.amount_charged,
                    provider_data=result.provider_data,
                )
            apply_payment_update(payment, update)
            await self.repository.save(payment)
        return result

    async def release_lock(self, payment: Payment, **kwargs) -> Decimal:
        """Release a pre-authorized lock."""
        context = self._run_operation_validators(
            operation="release_lock",
            payment=payment,
            kwargs=dict(kwargs),
        )
        processor = self.get_processor(payment)
        amount = await processor.release_lock(**context["kwargs"])
        apply_payment_update(
            payment,
            PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED),
        )
        await self.repository.save(payment)
        return amount

    async def start_refund(
        self,
        payment: Payment,
        amount: Decimal | None = None,
        **kwargs,
    ) -> RefundResult:
        """Start a refund."""
        context = self._run_operation_validators(
            operation="start_refund",
            payment=payment,
            kwargs={"amount": amount, **kwargs},
        )
        processor = self.get_processor(payment)
        result = await processor.start_refund(**context["kwargs"])
        apply_payment_update(
            payment,
            PaymentUpdate(
                payment_event=PaymentEvent.REFUND_REQUESTED,
                provider_data=result.provider_data,
            ),
        )
        await self.repository.save(payment)
        return result

    async def cancel_refund(self, payment: Payment, **kwargs) -> bool:
        """Cancel an in-progress refund."""
        context = self._run_operation_validators(
            operation="cancel_refund",
            payment=payment,
            kwargs=dict(kwargs),
        )
        processor = self.get_processor(payment)
        success = await processor.cancel_refund(**context["kwargs"])
        if success:
            apply_payment_update(
                payment,
                PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED),
            )
            await self.repository.save(payment)
        return success

    def get_processor(self, payment: Payment):
        """Instantiate the processor for a payment."""
        processor_class = self.registry.get_by_slug(payment.backend)
        backend_config = self.config.get(payment.backend, {})
        return processor_class(payment, config=backend_config)

    def _run_operation_validators(self, **context):
        return run_validators(context, validators=self.validators)
