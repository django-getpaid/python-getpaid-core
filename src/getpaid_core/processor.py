"""Base payment processor abstract class.

All payment backends subclass BaseProcessor and implement at minimum
prepare_transaction(). Other methods are optional depending on the
payment gateway's capabilities.
"""

from abc import ABC
from abc import abstractmethod
from decimal import Decimal

from getpaid_core.protocols import Payment
from getpaid_core.types import ChargeResponse
from getpaid_core.types import PaymentStatusResponse
from getpaid_core.types import TransactionResult


class BaseProcessor(ABC):
    """Base class for payment backend processors."""

    slug: str = ""
    display_name: str = ""
    accepted_currencies: list[str] = []
    logo_url: str | None = None
    sandbox_url: str = ""
    production_url: str = ""

    def __init__(self, payment: Payment, config: dict | None = None) -> None:
        self.payment = payment
        self.config = config or {}

    def get_setting(self, name: str, default=None):
        """Read a setting from backend config."""
        return self.config.get(name, default)

    def get_paywall_baseurl(self) -> str:
        """Return sandbox or production URL based on config."""
        sandbox = self.get_setting("sandbox", True)
        return self.sandbox_url if sandbox else self.production_url

    @abstractmethod
    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        """Prepare data for initiating a payment.

        Returns a TransactionResult that the framework adapter
        converts into an HTTP response.
        """
        ...

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Verify callback authenticity.

        Raise GetPaidException to reject. Default: no-op.
        """

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Handle async PUSH callback from payment gateway."""
        raise NotImplementedError

    async def fetch_payment_status(self, **kwargs) -> PaymentStatusResponse:
        """PULL flow: fetch payment status from gateway."""
        raise NotImplementedError

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResponse:
        """Charge a pre-authorized payment."""
        raise NotImplementedError

    async def release_lock(self, **kwargs) -> Decimal:
        """Release pre-authorized lock. Return locked amount."""
        raise NotImplementedError

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> Decimal:
        """Start a refund. Return refund amount."""
        raise NotImplementedError

    async def cancel_refund(self, **kwargs) -> bool:
        """Cancel in-progress refund. Return True if ok."""
        raise NotImplementedError
