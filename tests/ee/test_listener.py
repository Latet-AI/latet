"""What an instance hears: a message on a channel reaches that channel's handler, one that fails does not stop the
next, a dropped connection is listened to again, and stopping is at once. And the count of users, asked again after
a few seconds or when it is dropped."""

import asyncio
import logging
import threading
import time

from latet.ee.listener import Listener
from latet.ee.server import REFRESH_SECONDS, Counted

from .licenses import Clock


class FakeBus:
    """E0b's Bus as the listener uses it: listen(*channels) yields (channel, message); send() puts one in."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.listening = threading.Event()
        self.channels: tuple[str, ...] = ()
        self._loop = None
        self._queue = None

    async def listen(self, *channels):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("Valkey is restarting")
        self.channels = channels
        self._loop, self._queue = asyncio.get_running_loop(), asyncio.Queue()
        self.listening.set()
        while True:
            yield await self._queue.get()

    def send(self, channel, message):
        assert self.listening.wait(5), "the listener never listened"
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (channel, message))


def _until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "it did not happen in time"
        time.sleep(0.01)


def test_a_message_reaches_its_channels_handler_with_the_prefix_or_without():
    bus, heard = FakeBus(), []
    listener = Listener(bus, {"ee:license": heard.append, "identities": lambda m: heard.append(("identities", m))})
    listener.start()
    try:
        bus.send("ee:license", {"change": "license.installed"})
        bus.send("latet:identities", {"id": "u_7f3a2b9c1d"})
        _until(lambda: len(heard) == 2)
        assert heard == [{"change": "license.installed"}, ("identities", {"id": "u_7f3a2b9c1d"})]
        assert bus.channels == ("ee:license", "identities")
    finally:
        listener.stop()


def test_a_handler_that_fails_does_not_stop_the_next_message():
    bus, heard = FakeBus(), []

    def handler(message):
        if message.get("broken"):
            raise RuntimeError("cannot apply it")
        heard.append(message)

    listener = Listener(bus, {"ee:settings": handler})
    listener.start()
    try:
        bus.send("ee:settings", {"broken": True})
        bus.send("ee:settings", {"section": "demo"})
        _until(lambda: heard == [{"section": "demo"}])
    finally:
        listener.stop()


def test_a_connection_that_drops_is_listened_to_again():
    bus, heard = FakeBus(failures=2), []
    listener = Listener(bus, {"ee:license": heard.append}, retry=0.01)
    listener.start()
    try:
        bus.send("ee:license", {"change": "license.removed"})
        _until(lambda: heard == [{"change": "license.removed"}])
    finally:
        listener.stop()


def test_stopping_is_at_once_while_waiting_for_a_message():
    bus = FakeBus()
    listener = Listener(bus, {"ee:license": lambda message: None})
    listener.start()
    assert bus.listening.wait(5)
    started = time.monotonic()
    listener.stop()
    assert time.monotonic() - started < 2 and not listener.alive()


def test_a_cancellation_that_is_no_stop_is_said_and_listened_through(caplog):
    """Only stop() ends the listener: a cancellation from elsewhere (a driver's own) is said by its type, and the
    instance listens again rather than hearing nothing until it restarts."""

    class Cancelling(FakeBus):
        async def listen(self, *channels):
            if self.failures:
                self.failures -= 1
                raise asyncio.CancelledError
            async for heard in super().listen(*channels):
                yield heard

    bus, heard = Cancelling(failures=1), []
    listener = Listener(bus, {"ee:license": heard.append}, retry=0.01)
    with caplog.at_level(logging.WARNING, logger="latet.ee.listener"):
        listener.start()
        try:
            bus.send("ee:license", {"change": "license.installed"})
            _until(lambda: heard == [{"change": "license.installed"}])
            assert listener.alive()
        finally:
            listener.stop()
    assert any("CancelledError" in record.getMessage() for record in caplog.records)
    assert not listener.alive()


def test_the_count_is_asked_again_after_a_few_seconds_or_when_it_is_dropped():
    answers, ticks = iter([3, 4, 5]), Clock(0.0)
    count = Counted(lambda: next(answers), ticker=ticks)
    assert count() == 3 and count() == 3
    ticks.now += REFRESH_SECONDS
    assert count() == 4
    count.drop()
    assert count() == 5


def test_what_is_not_heard_and_a_handler_that_fails_are_logged_by_their_type_alone(caplog):
    """Valkey's words can name its address and its password; a handler's can quote what it was applying."""

    class Refusing(FakeBus):
        async def listen(self, *channels):
            if self.failures:
                self.failures -= 1
                raise ConnectionError("Error 111 connecting to redis://:hunter2@10.0.0.5:6379")
            async for heard in super().listen(*channels):
                yield heard

    bus, heard = Refusing(failures=1), []

    def handler(message):
        if message.get("broken"):
            raise RuntimeError("cannot apply Project Falcon")
        heard.append(message)

    listener = Listener(bus, {"ee:settings": handler}, retry=0.01)
    with caplog.at_level(logging.WARNING, logger="latet.ee.listener"):
        listener.start()
        try:
            bus.send("ee:settings", {"broken": True})
            bus.send("ee:settings", {"section": "demo"})
            _until(lambda: heard == [{"section": "demo"}])
        finally:
            listener.stop()
    said = [(record.getMessage(), record.exc_info) for record in caplog.records]
    assert any("ConnectionError" in line for line, _ in said) and any("RuntimeError" in line for line, _ in said)
    for line, exc_info in said:
        assert "hunter2" not in line and "10.0.0.5" not in line and "Falcon" not in line and exc_info is None, line
