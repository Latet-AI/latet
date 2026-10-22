import pytest

# contracts.py is no test module: without this, a failing comparison in a contract shows no operands.
pytest.register_assert_rewrite("tests.unit.state.contracts")
