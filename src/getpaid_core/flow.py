"""Payment flow orchestrator.

The main entry point for framework adapters. Orchestrates the
interaction between repository, processor, and state machine.
"""

from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.fsm import ALLOWED_CALLBACKS
from getpaid_core.fsm import create_fraud_machine
from getpaid_core.fsm import create_payment_machine
from getpaid_core.protocols import Order
from getpaid_core.protocols import Payment
from getpaid_core.protocols import PaymentRepository
from getpaid_core.registry import registry
from getpaid_core.types import TransactionResult
from getpaid_core.validators import run_validators


class PaymentFlow:
    """Core payment processing orchestrator.

    Framework adapters create an instance with their repository
    and backend configuration, then delegate to its methods.
    """

    def __init__(
        self,
        repository: PaymentRepository,
        config: dict | None = None,
        validators: list | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or {}
        self.validators = validators or []

    async def create_payment(
        self, order: Order, backend_slug: str, **kwargs
    ) -> Payment:
        """Create a new payment for an order."""
        registry.get_by_slug(backend_slug)
        payment = await self.repository.create(
            order=order,
            backend=backend_slug,
            amount_required=order.get_total_amount(),
            currency=order.get_currency(),
            description=order.get_description(),
            **kwargs,
        )
        return payment

    async def prepare(self, payment: Payment, **kwargs) -> TransactionResult:
        """Prepare a payment for processing.

        Runs validators, calls the backend's prepare_transaction(),
        transitions to PREPARED, and persists.
        """
        run_validators({"payment": payment}, validators=self.validators)
        create_payment_machine(payment)
        processor = self._get_processor(payment)
        result = await processor.prepare_transaction(**kwargs)
        payment.confirm_prepared()  # ty: ignore[unresolved-attribute]  # FSM trigger attached by create_payment_machine
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
        processor = self._get_processor(payment)
        await processor.verify_callback(data, headers, **kwargs)
        create_payment_machine(payment)
        create_fraud_machine(payment)
        await processor.handle_callback(data, headers, **kwargs)
        await self.repository.save(payment)

    async def fetch_and_update_status(self, payment: Payment) -> Payment:
        """PULL flow: fetch status from gateway and update."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        create_fraud_machine(payment)
        response = await processor.fetch_payment_status()
        if response.get("status"):
            callback = response["status"]
            if callback not in ALLOWED_CALLBACKS:
                raise InvalidTransitionError(
                    f"Callback {callback!r} not in ALLOWED_CALLBACKS"
                )
            trigger = getattr(payment, callback, None)
            if trigger and callable(trigger):
                trigger_kwargs = {}
                amount = response.get("amount")
                if amount is not None:
                    trigger_kwargs["amount"] = amount
                trigger(**trigger_kwargs)
        await self.repository.save(payment)
        return payment

    async def charge(self, payment: Payment, amount=None, **kwargs):
        """Charge a pre-authorized payment."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        result = await processor.charge(amount=amount, **kwargs)
        if result["success"]:
            payment.confirm_charge_sent()  # ty: ignore[unresolved-attribute]  # FSM trigger attached by create_payment_machine
        await self.repository.save(payment)
        return result

    async def release_lock(self, payment: Payment, **kwargs):
        """Release a pre-authorized lock."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        amount = await processor.release_lock(**kwargs)
        payment.release_lock()  # ty: ignore[unresolved-attribute]  # FSM trigger attached by create_payment_machine
        await self.repository.save(payment)
        return amount

    async def start_refund(self, payment: Payment, amount=None, **kwargs):
        """Start a refund."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        refund_amount = await processor.start_refund(amount=amount, **kwargs)
        payment.start_refund()  # ty: ignore[unresolved-attribute]  # FSM trigger attached by create_payment_machine
        await self.repository.save(payment)
        return refund_amount

    async def cancel_refund(self, payment: Payment, **kwargs):
        """Cancel an in-progress refund."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        success = await processor.cancel_refund(**kwargs)
        if success:
            payment.cancel_refund()  # ty: ignore[unresolved-attribute]  # FSM trigger attached by create_payment_machine
            await self.repository.save(payment)
        return success

    def _get_processor(self, payment: Payment):
        """Instantiate the processor for a payment."""
        processor_class = registry.get_by_slug(payment.backend)
        backend_config = self.config.get(payment.backend, {})
        return processor_class(payment, config=backend_config)
