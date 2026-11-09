"""``shielded``: a wait whose waiter may go, as ``asyncio.shield`` was before Python 3.14."""

import asyncio

import pytest

from latet.waiting import shielded


async def test_a_waiter_that_goes_leaves_the_work_and_its_failure_to_the_work_s_own_readers():
    """The waiter cancelled, the work goes on; what it raises later is its own callback's to read, and the loop's
    exception handler hears nothing (3.14's ``asyncio.shield`` logs it there as an error)."""
    loop = asyncio.get_running_loop()
    heard, read = [], []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: heard.append(context))
    try:
        work = loop.create_future()
        work.add_done_callback(lambda done: read.append(done.exception()))

        async def request():
            return await shielded(work)

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not work.done()
        work.set_exception(RuntimeError("the store did not answer"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [str(error) for error in read] == ["the store did not answer"] and heard == []
    finally:
        loop.set_exception_handler(previous)


def test_work_given_its_result_once_the_waiter_went_and_the_loop_closed_raises_nothing():
    """No callback of the wait is left on the work once its waiter has gone (3.14's shield leaves one, which a
    result given after the loop closed runs into: ``Event loop is closed``)."""
    loop = asyncio.new_event_loop()
    try:
        work = loop.create_future()

        async def wait():
            return await shielded(work)

        waiter = loop.create_task(wait())
        loop.call_soon(waiter.cancel)
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(waiter)
    finally:
        loop.close()
    work.set_result("decided")
    assert work.result() == "decided"


async def test_the_waiter_gets_what_the_work_gives_raises_or_whether_it_was_cancelled():
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    done.set_result(1)
    assert shielded(done) is done  # finished already: nothing to shield
    given, failed, cancelled = loop.create_future(), loop.create_future(), loop.create_future()
    waits = [asyncio.ensure_future(shielded(work)) for work in (given, failed, cancelled)]
    given.set_result("answer")
    failed.set_exception(ValueError("no"))
    cancelled.cancel()
    outcome = await asyncio.gather(*waits, return_exceptions=True)
    assert outcome[0] == "answer" and type(outcome[1]) is ValueError
    assert type(outcome[2]) is asyncio.CancelledError
