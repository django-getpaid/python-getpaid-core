"""Payment state machine using the transitions library.

Defines all valid payment and fraud status transitions.
The machine attaches trigger methods directly to payment objects.
"""

from transitions import Machine
from transitions.core import MachineError

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus


def _require_fully_paid(event_data):
    """Guard that raises MachineError when payment is not fully paid."""
    model = event_data.model
    if not model.is_fully_paid():
        raise MachineError(
            f"Transition '{event_data.event.name}' requires full payment."
        )


def _require_fully_refunded(event_data):
    """Guard that raises MachineError when payment is not fully refunded."""
    model = event_data.model
    if not model.is_fully_refunded():
        raise MachineError(
            f"Transition '{event_data.event.name}' requires full refund."
        )


PAYMENT_TRANSITIONS = [
    {
        "trigger": "confirm_prepared",
        "source": PaymentStatus.NEW,
        "dest": PaymentStatus.PREPARED,
    },
    {
        "trigger": "confirm_lock",
        "source": [PaymentStatus.NEW, PaymentStatus.PREPARED],
        "dest": PaymentStatus.PRE_AUTH,
    },
    {
        "trigger": "confirm_charge_sent",
        "source": PaymentStatus.PRE_AUTH,
        "dest": PaymentStatus.IN_CHARGE,
    },
    {
        "trigger": "confirm_payment",
        "source": [
            PaymentStatus.PRE_AUTH,
            PaymentStatus.PREPARED,
            PaymentStatus.IN_CHARGE,
            PaymentStatus.PARTIAL,
        ],
        "dest": PaymentStatus.PARTIAL,
    },
    {
        "trigger": "mark_as_paid",
        "source": PaymentStatus.PARTIAL,
        "dest": PaymentStatus.PAID,
        "before": _require_fully_paid,
    },
    {
        "trigger": "release_lock",
        "source": PaymentStatus.PRE_AUTH,
        "dest": PaymentStatus.REFUNDED,
    },
    {
        "trigger": "start_refund",
        "source": [
            PaymentStatus.PAID,
            PaymentStatus.PARTIAL,
        ],
        "dest": PaymentStatus.REFUND_STARTED,
    },
    {
        "trigger": "cancel_refund",
        "source": PaymentStatus.REFUND_STARTED,
        "dest": PaymentStatus.PARTIAL,
    },
    {
        "trigger": "confirm_refund",
        "source": PaymentStatus.REFUND_STARTED,
        "dest": PaymentStatus.PARTIAL,
    },
    {
        "trigger": "mark_as_refunded",
        "source": PaymentStatus.PARTIAL,
        "dest": PaymentStatus.REFUNDED,
        "before": _require_fully_refunded,
    },
    {
        "trigger": "fail",
        "source": [
            PaymentStatus.NEW,
            PaymentStatus.PRE_AUTH,
            PaymentStatus.PREPARED,
        ],
        "dest": PaymentStatus.FAILED,
    },
]

FRAUD_TRANSITIONS = [
    {
        "trigger": "flag_as_fraud",
        "source": FraudStatus.UNKNOWN,
        "dest": FraudStatus.REJECTED,
    },
    {
        "trigger": "flag_as_legit",
        "source": FraudStatus.UNKNOWN,
        "dest": FraudStatus.ACCEPTED,
    },
    {
        "trigger": "flag_for_check",
        "source": FraudStatus.UNKNOWN,
        "dest": FraudStatus.CHECK,
    },
    {
        "trigger": "mark_as_fraud",
        "source": FraudStatus.CHECK,
        "dest": FraudStatus.REJECTED,
    },
    {
        "trigger": "mark_as_legit",
        "source": FraudStatus.CHECK,
        "dest": FraudStatus.ACCEPTED,
    },
]

ALLOWED_CALLBACKS: frozenset[str] = frozenset(
    {
        "confirm_prepared",
        "confirm_lock",
        "confirm_charge_sent",
        "confirm_payment",
        "mark_as_paid",
        "release_lock",
        "start_refund",
        "cancel_refund",
        "confirm_refund",
        "mark_as_refunded",
        "fail",
    }
)


def create_payment_machine(payment) -> Machine:
    """Attach payment FSM to a payment object.

    The transitions library adds trigger methods directly to the
    object (confirm_prepared, confirm_lock, fail, etc.).
    """
    return Machine(
        model=payment,
        states=PaymentStatus,
        transitions=PAYMENT_TRANSITIONS,
        initial=payment.status or PaymentStatus.NEW,
        model_attribute="status",
        auto_transitions=False,
        send_event=True,
    )


def create_fraud_machine(payment) -> Machine:
    """Attach fraud status FSM to a payment object."""
    return Machine(
        model=payment,
        states=FraudStatus,
        transitions=FRAUD_TRANSITIONS,
        initial=payment.fraud_status or FraudStatus.UNKNOWN,
        model_attribute="fraud_status",
        auto_transitions=False,
    )
