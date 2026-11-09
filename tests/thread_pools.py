"""What a ThreadPoolExecutor keeps of a call submitted to it, read as each Python keeps it, for the tests that check
what a pool's work item, worker frames and worker thread own. Python 3.14 keeps the call as one ``task`` (``fn``,
``args``, ``kwargs``) that the worker's context runs, in a frame of its own (``WorkerContext.run``) between the work
item's and the call's, and starts each worker with that context where 3.12 and 3.13 pass it the initializer and its
arguments. The checks read a running frame's locals through ``frame_locals``, which leaves nothing on the frame."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from concurrent.futures import thread as native
from types import FrameType
from typing import Any

CONTEXTS = sys.version_info >= (3, 14)  # the call kept as one task, run by the worker's context
# The pool's frames between a work item's own (``_WorkItem.run``) and the call it runs, innermost first.
RUN_CODES: tuple[Any, ...] = (vars(native)["WorkerContext"].run.__code__,) if CONTEXTS else ()


def call(item: Any) -> tuple[Any, tuple[Any, ...], dict[str, Any]]:
    """The call a work item holds: its function, arguments and keywords."""
    if CONTEXTS:
        fn, args, kwargs = vars(item)["task"]
        return fn, args, kwargs
    held = vars(item)
    return held["fn"], held["args"], held["kwargs"]


def item_init(original: Callable[..., None], seen: Callable[[Any, Any, Any, tuple[Any, ...], dict[str, Any]], None]):
    """A ``_WorkItem.__init__`` that makes the item as ``original`` does, then hands ``seen`` the item, its future and
    the call it holds."""
    if CONTEXTS:

        def made(item: Any, future: Any, task: tuple[Any, tuple[Any, ...], dict[str, Any]]) -> None:
            original(item, future, task)
            seen(item, future, *task)

    else:

        def made(item: Any, future: Any, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            original(item, future, fn, args, kwargs)
            seen(item, future, fn, args, kwargs)

    return made


def frame_locals(frame: FrameType) -> dict[str, Any]:
    """A frame's locals in a dict of the caller's own, with nothing left on the frame: for a check on a frame that goes
    on running. Python 3.12's ``f_locals`` of a function's frame is a dict that the read makes and keeps on the frame,
    with each value in it, until the frame ends or is read again; on a frame that outlives the read, such as a pool
    worker's ``_worker`` (it ends with its thread), it keeps the work item the worker has deleted since, and the call
    that item held. 3.13's is a view that keeps nothing (PEP 667). A module's or a class body's ``f_locals`` is its
    namespace itself, never emptied. On 3.12 the dict a function's ``f_locals`` returns is also the one the frame's own
    ``locals()`` returns, so the clear would take names from a frame that keeps that dict or ``exec``s into it; the
    frames these tests read do neither."""
    view = frame.f_locals
    held = dict(view)
    if sys.version_info < (3, 13) and frame.f_code.co_flags & inspect.CO_OPTIMIZED:
        view.clear()  # the frame's own cache only: with no trace function, nothing writes it back into the frame
    return held


def item_frame(frame: FrameType | None, item: Any = None, *, called: bool = True) -> FrameType:
    """The work item's own frame (``_WorkItem.run``), from the pool's first frame above the call it runs (the call's
    ``f_back``), past the frames the pool runs the call in (``RUN_CODES``), each checked; with ``item``, the frames are
    checked to be that item's: on 3.14 the context's frame runs the item's very task. ``called`` false: from above
    the item's future's ``set_result``, which the work item's frame calls itself."""
    for code in RUN_CODES if called else ():
        assert frame is not None and frame.f_code is code, "not the frame the pool runs a call in"
        if item is not None:
            assert frame_locals(frame)["task"] is vars(item)["task"]
        frame = frame.f_back
    assert frame is not None and frame.f_code is native._WorkItem.run.__code__, "not a work item's frame"
    if item is not None:
        assert frame_locals(frame)["self"] is item
    return frame


def worker_context(args: tuple[Any, ...], executor_ref: Any, queue: Any) -> Any:
    """Checks a worker thread's arguments: its executor's weak reference and the pool's queue, and no initializer (3.12
    and 3.13: ``None, ()``; 3.14: a context with none). The context on 3.14, None before."""
    if CONTEXTS:
        assert len(args) == 3 and args[0] is executor_ref and args[2] is queue, args
        context = args[1]
        assert type(context) is vars(native)["WorkerContext"]
        assert vars(context) == {"initializer": None, "initargs": ()}, vars(context)
        return context
    assert len(args) == 4 and args[0] is executor_ref and args[1] is queue and args[2:] == (None, ()), args
    return None


def initialize_with(pool: Any, initializer: Callable[[], None], monkeypatch: Any = None) -> None:
    """The workers ``pool`` starts from now on run ``initializer`` first, as one given to the pool would."""
    if CONTEXTS:
        name, value = "_create_worker_context", vars(native)["WorkerContext"].prepare(initializer, ())[0]
    else:
        name, value = "_initializer", initializer
    if monkeypatch is None:
        setattr(pool, name, value)
    else:
        monkeypatch.setattr(pool, name, value)
