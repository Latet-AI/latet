"""Bounded, detached model-output records submitted to the mediator's existing engine executor."""

import logging
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from threading import Lock

from latet.protocols.rewrite import _utf8_size
from latet.protocols.sse import ResponseError

log = logging.getLogger(__name__)

MAX_ACCEPTED_BYTES = 32 * 1024 * 1024
MAX_ACCEPTED_BATCHES = 256
_BATCH_BYTES = 128
_PAIR_BYTES = 128
_NAME_BYTES = 64


@dataclass(slots=True)
class _Records:
    remembered: list[tuple[str, str]]
    named: list[str]
    remember: Callable[[str, str], None]
    name: Callable[[str], None]
    _size: int | None = None

    def __post_init__(self) -> None:
        if self._size is None:
            self._size = (
                _BATCH_BYTES
                + sum(_utf8_size(masked) + _utf8_size(restored) + _PAIR_BYTES for masked, restored in self.remembered)
                + sum(_utf8_size(response_id) + _NAME_BYTES for response_id in self.named)
            )

    @property
    def byte_size(self) -> int:
        assert self._size is not None
        return self._size

    def discard(self) -> None:
        self.remembered.clear()
        self.named.clear()


def _record(records: _Records) -> None:
    try:
        for masked, restored in records.remembered:
            records.remember(masked, restored)
        for response_id in records.named:
            records.name(response_id)
    finally:
        records.discard()


class _AcceptedProvenance:
    """One mediator's accepted records, including running work. Only its existing engine executor does store I/O.

    Production supplies Mediator's ThreadPoolExecutor; an executor that runs submitted callbacks inline is unsupported.
    """

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.closed = False
        self.held_bytes = 0
        self.pending_batches = 0
        self.pending: dict[Future[None], tuple[_Records, int]] = {}
        # Only bounded metadata and submit/rollback are protected. No payload, callback, Future operation or wait.
        self._metadata = Lock()
        self._reported = False

    def submit(self, records: _Records) -> Future[None] | None:
        if not records.remembered and not records.named:
            return None
        charge = records.byte_size
        future = None
        with self._metadata:
            if (
                not self.closed
                and self.held_bytes + charge <= MAX_ACCEPTED_BYTES
                and self.pending_batches < MAX_ACCEPTED_BATCHES
            ):
                self.held_bytes += charge
                self.pending_batches += 1
                try:
                    future = self.executor.submit(_record, records)
                except Exception:
                    self.held_bytes -= charge
                    self.pending_batches -= 1
                else:
                    self.pending[future] = (records, charge)
        if future is None:
            records.discard()
            raise ResponseError("the provider's output could not be recorded within Latet's limits")
        # Registration is outside the mutex: a Future already finished/cancelled invokes this inline.
        future.add_done_callback(self._finished)
        return future

    def cancel(self, future: Future[None]) -> bool:
        if not future.cancel():
            return False  # a running worker owns its records through its own finally
        self._finished(future)  # cleanup also works if the completion observer is not registered yet
        return True

    def _finished(self, future: Future[None]) -> None:
        with self._metadata:
            entry = self.pending.pop(future, None)
        if entry is None:
            return
        records, charge = entry
        error = None
        try:
            if future.cancelled():
                records.discard()  # confirmed pre-start cancellation; the executor may retain its work item
            else:
                error = future.exception()  # the worker's finally already cleared the payload
        finally:
            # Keep the reservation until payload ownership has ended, even during concurrent cancellation/close.
            with self._metadata:
                self.held_bytes -= charge
                self.pending_batches -= 1
        if error is not None:
            with self._metadata:
                report = not self._reported
                self._reported = True
            if report:
                log.warning("accepted model-output recording failed: %s", type(error).__name__)

    def close(self) -> None:
        with self._metadata:
            self.closed = True
            pending = tuple(self.pending)
        for future in pending:
            self.cancel(future)
