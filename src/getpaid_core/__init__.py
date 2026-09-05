"""Getpaid Core -- framework-agnostic payment processing."""

__version__ = "3.2.0"

from getpaid_core.enums import AutoName
from getpaid_core.enums import BackendMethod
from getpaid_core.enums import ConfirmationMethod
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import BackendNotFoundError
from getpaid_core.exceptions import ChargeFailure
from getpaid_core.exceptions import CommunicationError
from getpaid_core.exceptions import ConformanceError
from getpaid_core.exceptions import CredentialsError
from getpaid_core.exceptions import GetPaidException
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import LockFailure
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.exceptions import ReconciliationRequiredError
from getpaid_core.exceptions import RefundFailure
from getpaid_core.exceptions import StateConflictError
from getpaid_core.exceptions import UnsupportedRepositoryError
from getpaid_core.flow import PaymentFlow
from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import PluginRegistry
from getpaid_core.registry import registry
from getpaid_core.types import ChargeResult
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult


__all__ = [
    "AutoName",
    "BackendMethod",
    "BackendNotFoundError",
    "BaseProcessor",
    "ChargeFailure",
    "ChargeResult",
    "CommunicationError",
    "ConfirmationMethod",
    "ConformanceError",
    "CredentialsError",
    "FraudEvent",
    "FraudStatus",
    "GetPaidException",
    "InvalidCallbackError",
    "InvalidTransitionError",
    "LockFailure",
    "OperationConflictError",
    "PaymentEvent",
    "PaymentFlow",
    "PaymentStatus",
    "PaymentUpdate",
    "PluginRegistry",
    "ReconciliationRequiredError",
    "RefundFailure",
    "RefundResult",
    "StateConflictError",
    "TransactionResult",
    "UnsupportedRepositoryError",
    "__version__",
    "registry",
]
