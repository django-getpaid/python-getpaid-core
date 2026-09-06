"""Base payment processor abstract class."""

from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar

from getpaid_core.protocols import Payment
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult


if TYPE_CHECKING:
    from getpaid_core.durable.provider import OperationCapabilities
    from getpaid_core.durable.provider import OperationNotFound
    from getpaid_core.durable.records import OperationOutcome
    from getpaid_core.durable.records import OperationRecord
    from getpaid_core.durable.records import OperationType


class BaseProcessor(ABC):
    """Base class for payment backend processors."""

    slug: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    accepted_currencies: ClassVar[Sequence[str]] = ()
    logo_url: ClassVar[str | None] = None
    sandbox_url: ClassVar[str] = ""
    production_url: ClassVar[str] = ""

    def __init__(
        self, payment: Payment, config: dict[str, Any] | None = None
    ) -> None:
        self.payment = payment
        self.config = dict(config or {})

    operation_capabilities: ClassVar[
        Mapping["OperationType", "OperationCapabilities"]
    ] = {}

    @classmethod
    async def submit_operation(
        cls, operation: "OperationRecord", *, config: Mapping[str, Any]
    ) -> "OperationOutcome":
        """Submit exactly the frozen intent, using its idempotency key.

        Durable commands deliberately bypass the instance's mutable payment.
        All request-specific inputs must come from the reservation, including
        provider correlation for a targeted cancellation. Config supplies only
        deployment settings/credentials, whose provider account must stay stable
        throughout an intent's lifetime. Never read current balances to
        construct a retry payload. Return normalized acceptance/settlement
        evidence; communication uncertainty is not rejection.
        """
        raise NotImplementedError

    @classmethod
    async def lookup_operation(
        cls, operation: "OperationRecord", *, config: Mapping[str, Any]
    ) -> "OperationOutcome | OperationNotFound":
        """Query evidence tied to this operation, not an arbitrary active one.

        Return OperationNotFound for absence. It is UNKNOWN unless the contract
        conclusively excludes execution. Delta-only uncorrelated evidence must
        return UNKNOWN with reconciliation_required, never guessed totals.
        """
        raise NotImplementedError

    def get_setting(self, name: str, default: Any = None) -> Any:
        """Read a setting from backend config."""
        return self.config.get(name, default)

    def get_paywall_baseurl(self) -> str:
        """Return sandbox or production URL based on config."""
        sandbox = self.get_setting("sandbox", True)
        return self.sandbox_url if sandbox else self.production_url

    @abstractmethod
    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        """Prepare data for initiating a payment."""

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Verify callback authenticity.

        Fail-closed default: raises ``NotImplementedError``. Every
        processor must implement provider callback authentication (e.g.
        signature or HMAC verification) and raise
        :class:`~getpaid_core.exceptions.InvalidCallbackError` on failure.
        If the provider genuinely offers no verification mechanism,
        override this method explicitly with a documented no-op.
        """
        raise NotImplementedError(
            "processor must implement verify_callback to authenticate "
            "provider callbacks; override explicitly if the provider has "
            "no verification"
        )

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> PaymentUpdate | None:
        """Handle async PUSH callback from payment gateway."""
        raise NotImplementedError

    async def fetch_payment_status(self, **kwargs) -> PaymentUpdate | None:
        """PULL flow: fetch payment status from gateway."""
        raise NotImplementedError

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResult:
        """Charge a pre-authorized payment."""
        raise NotImplementedError

    async def release_lock(self, **kwargs) -> Decimal:
        """Release pre-authorized lock. Return released amount."""
        raise NotImplementedError

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> RefundResult:
        """Start a refund. Return refund metadata."""
        raise NotImplementedError

    async def cancel_refund(self, **kwargs) -> bool:
        """Cancel in-progress refund. Return True if ok."""
        raise NotImplementedError
