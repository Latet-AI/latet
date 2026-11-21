"""Secrets of the Enterprise settings, a server's alone: a name is a plain name, never a path nor a name Windows keeps
for a device, and a value that cannot be kept is refused in words that hold nothing of it."""

import pytest

from latet.ee.secrets import MAX_BYTES, secret_name, value_problem


def test_a_secret_larger_than_a_secret_holds_is_refused_without_quoting_it():
    problem = value_problem("s" * (MAX_BYTES + 1))
    assert problem == f"a secret holds {MAX_BYTES // 1024} KB at most" and "sss" not in problem
    assert value_problem("s" * MAX_BYTES) is None


def test_a_secret_that_is_not_text_is_refused_holding_nothing_of_it():
    value = "client-" + chr(0xD800) + "secret"  # a lone surrogate: UTF-8 cannot write it, and its error holds it all
    problem = value_problem(value)
    assert problem is not None and problem.startswith("a secret is text") and "client" not in problem
    assert value_problem(b"client-secret") == "a secret is text" and value_problem(None) == "a secret is text"


@pytest.mark.parametrize("name", ["../root.key", "a/b", "", "SSO", "x" * 64])
def test_a_secrets_name_is_a_plain_name(name):
    with pytest.raises(ValueError, match="lowercase letters, digits and hyphens"):
        secret_name(name)


@pytest.mark.parametrize("name", ["con", "prn", "aux", "nul", "com0", "com1", "com9", "lpt0", "lpt1", "lpt9"])
def test_a_secrets_name_is_never_a_windows_device(name):
    with pytest.raises(ValueError, match="Windows keeps for a device"):
        secret_name(name)
    assert secret_name(f"{name}-1") == f"{name}-1"  # only the name itself is a device's
