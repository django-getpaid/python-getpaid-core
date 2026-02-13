"""Tests for getpaid_core.validators."""

import pytest

from getpaid_core.exceptions import GetPaidException
from getpaid_core.validators import run_validators


class TestRunValidators:
    def test_no_validators(self):
        """No validators means no error."""
        run_validators({"amount": 100}, validators=[])

    def test_single_passing_validator(self):
        def ok_validator(data):
            return data

        run_validators({"amount": 100}, validators=[ok_validator])

    def test_validator_can_modify_data(self):
        """Validators receive and return data (pipeline)."""

        def add_field(data):
            data["extra"] = True
            return data

        data = {"amount": 100}
        result = run_validators(data, validators=[add_field])
        assert result["extra"] is True

    def test_chain_of_validators(self):
        def add_a(data):
            data["a"] = 1
            return data

        def add_b(data):
            data["b"] = 2
            return data

        result = run_validators({}, validators=[add_a, add_b])
        assert result == {"a": 1, "b": 2}

    def test_failing_validator_raises(self):
        def fail_validator(data):
            raise GetPaidException("invalid payment")

        with pytest.raises(GetPaidException, match="invalid payment"):
            run_validators({}, validators=[fail_validator])

    def test_validators_run_in_order(self):
        order = []

        def first(data):
            order.append(1)
            return data

        def second(data):
            order.append(2)
            return data

        run_validators({}, validators=[first, second])
        assert order == [1, 2]

    def test_default_no_validators(self):
        """Called with no validators argument defaults to empty."""
        result = run_validators({"x": 1})
        assert result == {"x": 1}
