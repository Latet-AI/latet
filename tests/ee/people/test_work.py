"""Work that refuses, in E0b's work threads: its refusals are raised again as they were, anything else is E0b's
StoresUnavailable (ee/work.py)."""

import pytest

from latet.ee.oidc import SignInError
from latet.ee.work import working
from latet.server.machines import StoresUnavailable


async def test_a_refusal_the_work_raises_is_raised_again_as_it_was():
    refused = SignInError("not-allowed", "not in the access group")

    def work(person):
        assert person == "Emily Carter"
        raise refused

    with pytest.raises(SignInError) as caught:
        await working(work, "Emily Carter", refusals=(SignInError,))
    assert caught.value is refused and caught.value.code == "not-allowed"


async def test_anything_else_it_raises_is_a_server_that_cannot_answer():
    def work():
        raise KeyError("u_emilycartr")

    with pytest.raises(StoresUnavailable) as caught:
        await working(work, refusals=(SignInError,))
    assert not isinstance(caught.value, SignInError) and "u_emilycartr" not in str(caught.value)


async def test_what_the_work_answers_is_answered():
    assert await working(lambda a, b: a + b, 2, 3, refusals=(SignInError,)) == 5
