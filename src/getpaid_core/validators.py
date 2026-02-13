"""Pluggable payment validation system.

Validators are callables that receive a data dict, optionally
modify it, and return it. They raise GetPaidException to reject.
"""

from collections.abc import Callable


def run_validators(
    data: dict,
    validators: list[Callable] | None = None,
) -> dict:
    """Run a chain of validators on payment data.

    Each validator receives the data dict and must return it
    (possibly modified). Raise GetPaidException to reject.
    """
    for validator in validators or []:
        data = validator(data)
    return data
