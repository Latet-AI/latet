"""Waiting on the event loop for work that goes on without its waiter."""

from __future__ import annotations

import asyncio


def shielded[T](work: asyncio.Future[T]) -> asyncio.Future[T]:
    """``asyncio.shield(work)`` as Python 3.12 and 3.13 have it: a future that ends as ``work`` does, whose cancellation
    leaves ``work`` to go on, and to be read by whoever else waits on it or by its own done callbacks. Python 3.14's
    shield also hands what ``work`` raises once such a waiter has gone to the loop's exception handler, which logs it as
    an error, its traceback and causes with it, and it leaves a callback on ``work`` for that: a result given to
    ``work`` once its loop is closed then raises."""
    if work.done():
        return work  # cancelling it does nothing
    view: asyncio.Future[T] = work.get_loop().create_future()

    def ended(done: asyncio.Future[T]) -> None:
        if view.done():
            return
        if done.cancelled():
            view.cancel()
        elif (error := done.exception()) is not None:
            view.set_exception(error)
        else:
            view.set_result(done.result())

    def left(_: asyncio.Future[T]) -> None:
        if view.cancelled():
            work.remove_done_callback(ended)

    work.add_done_callback(ended)
    view.add_done_callback(left)
    return view
