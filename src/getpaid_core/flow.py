"""Payment flow orchestrator."""

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from getpaid_core._amounts import validate_amount
from getpaid_core._amounts import validate_payment_amounts
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import ReconciliationRequiredError
from getpaid_core.fsm import apply_payment_update
from getpaid_core.fsm import capturable_amount
from getpaid_core.fsm import coerce_payment_status
from getpaid_core.fsm import has_unresolved_refund
from getpaid_core.fsm import refundable_amount
from getpaid_core.fsm import require_capture_eligible
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


class BaseFlow:
    """What every flow shares: configuration, validators and processors.

    Concrete flows differ in the storage contract they orchestrate --
    :class:`PaymentFlow` over the released ``PaymentRepository``,
    ``DurablePaymentFlow`` over the durable one -- not in how they reach
    a processor or run operation validators.
    """

    def __init__(
        self,
        repository: Any,
        config: dict[str, dict[str, Any]] | None = None,
        validators: list[OperationValidator] | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.config: dict[str, dict[str, Any]] = config or {}
        self.validators: list[OperationValidator] = validators or []
        self.registry = registry or default_registry

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


class PaymentFlow(BaseFlow):
    """Core payment processing orchestrator.

    This is the released 3.x flow: it applies updates to the payment
    object the caller supplies and saves it. Two independent snapshots of
    one payment can overwrite each other's committed amounts, so it makes
    no atomicity guarantee across workers. Integrations that need one
    move to ``getpaid_core.durable.DurablePaymentFlow`` over a repository
    implementing the durable contract; see the durable storage contract
    documentation for the upgrade boundary.
    """

    def __init__(
        self,
        repository: PaymentRepository,
        config: dict[str, dict[str, Any]] | None = None,
        validators: list[OperationValidator] | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        super().__init__(repository, config, validators, registry)
        self.repository: PaymentRepository = repository

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
        """Prepare a payment for processing.

        Raises ``InvalidTransitionError`` when the payment is not NEW.
        The check runs before the processor call, so a rejected request
        leaves no orphaned order at the provider.
        """
        context = self._run_operation_validators(
            operation="prepare",
            payment=payment,
            kwargs=dict(kwargs),
        )
        status = coerce_payment_status(payment)
        if status is not PaymentStatus.NEW:
            raise InvalidTransitionError(
                f"Cannot prepare payment in {status.value!r} status. "
                "Payment must be NEW."
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
        """Capture funds from a payment's remaining authorization.

        Eligibility is the payment's current facts, not the status it
        happens to hold: a partial capture leaves the remaining
        authorization usable, so capture may be repeated until that
        authorization or the required amount is exhausted. Returned funds
        close it -- refunding does not reopen capture capacity, and
        collecting replacement funds needs a new payment.

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
        # Validate preconditions before calling the processor (avoids
        # unnecessary API calls when the payment is not chargeable).
        # Eligibility follows the payment's current facts, so a partial
        # capture leaves its remaining authorization usable.
        validate_payment_amounts(payment)
        require_capture_eligible(payment)
        available = capturable_amount(payment)
        if available <= 0:
            reason = (
                "no remaining authorization is held"
                if payment.amount_locked <= 0
                else "the required amount is already captured"
            )
            raise InvalidTransitionError(
                f"Cannot charge payment {payment.id!r}: {reason}."
            )
        amount = context["kwargs"].get("amount")
        if amount is None:
            amount = available
        validate_amount(
            amount, "Charge amount", allow_zero=False, maximum=available
        )
        context["kwargs"]["amount"] = amount
        processor = self.get_processor(payment)
        result = await processor.charge(**context["kwargs"])
        self._validate_provider_amount(
            payment,
            result.amount_charged,
            result,
            operation="charge",
            maximum=amount if result.success else Decimal("0"),
            allow_zero=not result.success or result.async_call,
        )
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
        """Release the payment's remaining authorization.

        A release needs a positive remaining authorization, not a
        particular status: the authorization left over after a partial
        capture is still releasable. It removes that authorization whole
        and changes neither captured nor refunded funds, so a payment
        with captured funds stays reported as paid or partially paid
        rather than refunded.
        """
        context = self._run_operation_validators(
            operation="release_lock",
            payment=payment,
            kwargs=dict(kwargs),
        )
        validate_payment_amounts(payment)
        if payment.amount_locked <= 0:
            raise InvalidTransitionError(
                f"Cannot release the authorization of payment "
                f"{payment.id!r}: none is held."
            )
        validate_amount(
            payment.amount_locked, "amount_locked", allow_zero=False
        )
        available = payment.amount_locked
        processor = self.get_processor(payment)
        amount = await processor.release_lock(**context["kwargs"])
        self._validate_provider_amount(
            payment, amount, amount, operation="release_lock", maximum=available
        )
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
        """Start a refund of captured funds.

        Eligibility is the captured funds not yet returned, so a
        partially refunded payment stays refundable down to zero.
        """
        context = self._run_operation_validators(
            operation="start_refund",
            payment=payment,
            kwargs={"amount": amount, **kwargs},
        )
        validate_payment_amounts(payment)
        available = refundable_amount(payment)
        if available <= 0:
            raise InvalidTransitionError(
                f"Cannot start refund for payment {payment.id!r}: no "
                "captured funds are left to return."
            )
        amount = context["kwargs"].get("amount")
        if amount is None:
            amount = available
        validate_amount(
            amount, "Refund amount", allow_zero=False, maximum=available
        )
        context["kwargs"]["amount"] = amount
        processor = self.get_processor(payment)
        result = await processor.start_refund(**context["kwargs"])
        self._validate_provider_amount(
            payment,
            result.amount,
            result,
            operation="start_refund",
            maximum=amount,
        )
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
        """Cancel an in-progress refund.

        Returns the processor's success flag. Raises
        ``InvalidTransitionError`` when the payment has no refund to
        cancel -- before the processor call, so an invalid cancellation
        never reaches the provider.
        """
        context = self._run_operation_validators(
            operation="cancel_refund",
            payment=payment,
            kwargs=dict(kwargs),
        )
        status = coerce_payment_status(payment)
        outstanding = (
            has_unresolved_refund(payment) or refundable_amount(payment) > 0
        )
        if not outstanding:
            raise InvalidTransitionError(
                f"Cannot cancel refund for payment in {status.value!r} "
                "status: no refund is outstanding and no captured funds "
                "remain to return."
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

    @staticmethod
    def _validate_provider_amount(
        payment: Payment,
        amount: Decimal,
        result: Any,
        *,
        operation: str,
        maximum: Decimal,
        allow_zero: bool = False,
    ) -> None:
        try:
            validate_amount(
                amount,
                f"{operation} result amount",
                allow_zero=allow_zero,
                maximum=maximum,
                maximum_name="requested amount",
            )
            if operation == "release_lock" and amount != maximum:
                raise InvalidTransitionError(
                    "Lock release result must cover the full authorization."
                )
        except InvalidTransitionError as exc:
            # The command already reached the provider. Do not treat an
            # invalid result as a safely rejected request or mutate state.
            context = {
                "payment_id": payment.id,
                "operation": operation,
                "provider_result": result,
            }
            charge_result = result if isinstance(result, ChargeResult) else None
            if charge_result is not None:
                context["charge_result"] = charge_result
            raise ReconciliationRequiredError(
                f"Gateway returned an invalid {operation} amount for payment "
                f"{payment.id!r}; manual reconciliation required.",
                context=context,
                charge_result=charge_result,
            ) from exc
