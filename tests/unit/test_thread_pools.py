"""tests/thread_pools.py's ``frame_locals``: a frame's locals, read for a check, with nothing left on the frame."""

import sys
import weakref

from ..thread_pools import frame_locals


def test_a_frames_locals_read_for_a_check_leave_nothing_on_the_frame():
    """Python 3.12's ``f_locals`` of a function's frame is a dict that the read makes and keeps on the frame, each value
    in it, until the frame ends or is read again (3.13's is a view, PEP 667). Read that way on a pool worker's
    ``_worker`` frame, which ends with its thread, it kept the work item the worker had since deleted."""

    class Payload:
        __slots__ = ("__weakref__",)

    def holder(payload):
        yield
        del payload
        yield

    payload = Payload()
    gone = weakref.ref(payload)
    suspended = holder(payload)
    del payload
    try:
        next(suspended)
        frame = suspended.gi_frame
        assert frame is not None
        held = frame_locals(frame)
        assert held["payload"] is gone() and type(held) is dict
        del held, frame
        next(suspended)  # the frame deletes its argument and lives on
        assert gone() is None, "the read kept what the frame has deleted since"
    finally:
        suspended.close()


def test_a_namespace_read_as_a_frames_locals_is_left_whole():
    """A module's or a class body's ``f_locals`` is its namespace itself, never a copy to empty."""
    namespace: dict[str, object] = {}
    exec("import sys\nframe = sys._getframe()\nkept = 1", namespace)
    frame = namespace["frame"]
    assert type(frame) is type(sys._getframe())
    held = frame_locals(frame)
    assert held["kept"] == 1 and held is not namespace
    assert namespace["kept"] == 1 and namespace["frame"] is frame and "sys" in namespace
    namespace.clear()  # the frame and its namespace hold each other
