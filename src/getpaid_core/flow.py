"""Payment flow orchestrator."""

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import ReconciliationRequiredError
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


logger = logging.getLogger(__name__)

OperationValidator = Callable[[dict[str, Any]], dict[str, Any]]


def _charge_log_summary(
    payment: Payment,
    result: ChargeResult,
) -> dict[str, Any]:
    """Build an allowlisted, log-safe summary of a charge outcome.

    Only core-owned, typed fields are included: payment and operation
    identity, the provider correlation handle, the outcome flags and the
    amounts. ``ChargeResult.provider_data`` is plugin-defined
    ``dict[str, Any]`` -- it may carry stored-credential tokens, buyer
    details or raw provider responses -- so no key or value of it is ever
    interpolated into a log record; only the number of entries is
    reported, as a hint that provider metadata exists. Core has no typed
    provider error field, so no provider error code is logged either.

    Full recovery evidence stays available to the caller through
    ``ReconciliationRequiredError.charge_result``, which is the
    controlled channel for sensitive provider metadata.
    """
    return {
        "payment_id": payment.id,
        "operation": "charge",
        "backend": payment.backend,
        "external_id": payment.external_id,
        "currency": payment.currency,
        "success": result.success,
        "async_call": result.async_call,
        "amount_charged": result.amount_charged,
        "amount_required": payment.amount_required,
        "provider_data_entries": len(result.provider_data),
    }


class PaymentFlow:
    """Core payment processing orchestrator."""

    def __init__(
        self,
        repository: PaymentRepository,
        config: dict[str, dict[str, Any]] | None = None,
        validators: list[OperationValidator] | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.config: dict[str, dict[str, Any]] = config or {}
        self.validators: list[OperationValidator] = validators or []
        self.registry = registry or default_registry

    async def create_payment(
        self,
        order: Order,
        backend_slug: str,
        **kwargs: Any,
    ) -> Payment:
        """Create a new payment for an order.

        Raises ``BackendNotFoundError`` (a ``KeyError`` subclass) when
        ``backend_slug`` does not match a registered backend.
        """
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

    async def prepare(
        self,
        payment: Payment,
        **kwargs: Any,
    ) -> TransactionResult:
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
        data: dict[str, Any],
        headers: dict[str, str],
        **kwargs: Any,
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

    async def fetch_and_update_status(
        self,
        payment: Payment,
    ) -> Payment:
        """PULL flow: fetch status from gateway and update."""
        context = self._run_operation_validators(
            operation="fetch_status",
            payment=payment,
            kwargs={},
        )
        processor = self.get_processor(payment)
        update = await processor.fetch_payment_status(**context["kwargs"])
        if update is None:
            return payment
        apply_payment_update(payment, update)
        await self.repository.save(payment)
        return payment

    async def charge(
        self,
        payment: Payment,
        amount: Decimal | None = None,
        **kwargs: Any,
    ) -> ChargeResult:
        """Charge a pre-authorized payment.

        Raises ``ReconciliationRequiredError`` when the gateway charge
        succeeded but recording it locally failed -- in that case money
        has moved at the provider and the payment needs manual
        reconciliation. A gateway-declined charge (``success=False``) is
        recorded on the payment as a FAILED event before returning.
        """
        context = self._run_operation_validators(
            operation="charge",
            payment=payment,
            kwargs={"amount": amount, **kwargs},
        )
        # Validate precondition before calling processor (avoids
        # unnecessary API calls when the payment is not chargeable).
        if payment.status not in {
            PaymentStatus.PRE_AUTH,
            PaymentStatus.IN_CHARGE,
        }:
            raise InvalidTransitionError(
                f"Cannot charge payment in {payment.status!r} status. "
                "Payment must be PRE_AUTH or IN_CHARGE."
            )
        processor = self.get_processor(payment)
        result = await processor.charge(**context["kwargs"])
        if not result.success:
            logger.warning(
                "Gateway declined charge: %s",
                _charge_log_summary(payment, result),
            )
            apply_payment_update(
                payment,
                PaymentUpdate(
                    payment_event=PaymentEvent.FAILED,
                    provider_data=result.provider_data,
                ),
            )
            await self.repository.save(payment)
            return result

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
        try:
            apply_payment_update(payment, update)
            await self.repository.save(payment)
        except Exception as exc:
            # Money moved at the gateway but we could not record it
            # locally -- surface a dedicated error for reconciliation
            # instead of the bare local failure.
            logger.critical(
                "Gateway charge succeeded but local update failed; "
                "manual reconciliation required (full provider result on "
                "ReconciliationRequiredError.charge_result): %s",
                _charge_log_summary(payment, result),
                exc_info=True,
            )
            raise ReconciliationRequiredError(
                f"Gateway charge succeeded for payment {payment.id!r} "
                "but the local update failed; manual reconciliation "
                "required.",
                context={
                    "payment_id": payment.id,
                    "charge_result": result,
                },
                charge_result=result,
            ) from exc
        return result

    async def release_lock(
        self,
        payment: Payment,
        **kwargs: Any,
    ) -> Decimal:
        """Release a pre-authorized lock."""
        context = self._run_operation_validators(
            operation="release_lock",
            payment=payment,
            kwargs=dict(kwargs),
        )
        if payment.status != PaymentStatus.PRE_AUTH:
            raise InvalidTransitionError(
                f"Cannot release lock for payment in {payment.status!r} "
                "status. Payment must be PRE_AUTH."
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
        **kwargs: Any,
    ) -> RefundResult:
        """Start a refund."""
        context = self._run_operation_validators(
            operation="start_refund",
            payment=payment,
            kwargs={"amount": amount, **kwargs},
        )
        if payment.status not in {
            PaymentStatus.PAID,
            PaymentStatus.PARTIAL,
            PaymentStatus.REFUND_STARTED,
        }:
            raise InvalidTransitionError(
                f"Cannot start refund for payment in {payment.status!r} "
                "status. Payment must be PAID, PARTIAL, or REFUND_STARTED."
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

    async def cancel_refund(
        self,
        payment: Payment,
        **kwargs: Any,
    ) -> bool:
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

    def get_processor(
        self,
        payment: Payment,
    ) -> Any:
        """Instantiate the processor for a payment.

        Raises ``BackendNotFoundError`` (a ``KeyError`` subclass) when
        the payment's backend is not registered.
        """
        processor_class = self.registry.get_by_slug(payment.backend)
        backend_config = self.config.get(payment.backend, {})
        return processor_class(payment, config=backend_config)

    def _run_operation_validators(
        self,
        **context: Any,
    ) -> dict[str, Any]:
        return run_validators(context, validators=self.validators)
