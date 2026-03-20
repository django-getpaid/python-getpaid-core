"""Pluggable validation and mutation hooks for payment operations."""

from collections.abc import Callable


def run_validators(
    data: dict,
    validators: list[Callable] | None = None,
) -> dict:
    """Run a chain of validators on operation context."""
    for validator in validators or []:
        data = validator(data)
    return data
