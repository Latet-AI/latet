"""latet.state's implementations on a computer (today's memory, SQLite and files), held to the contracts that a
server's implementations meet too (tests/server, E0b)."""

import ast
import asyncio
import contextlib
import hashlib
import threading
from pathlib import Path

import pytest

from latet.engine.pipeline import Session
from latet.gateway.approvals import Decision
from latet.gateway.aws import AwsError
from latet.gateway.recorder import Broadcaster
from latet.gateway.sigv4 import AwsCredentials
from latet.state import (
    CODE_TTL,
    SESSION_TTL,
    LocalApprovals,
    LocalAwsSessions,
    LocalConversations,
    LocalGiven,
    LocalJobLock,
    LocalSentBlocks,
    LocalSettings,
    LocalSignIns,
)
from latet.state import local as local_module
from latet.storage.db import Store
from latet.storage.exchanges import ExchangeStore

from ...conftest import ROOT_KEY
from .contracts import (
    ApprovalsContract,
    AwsSessionsContract,
    Clock,
    ConversationsContract,
    FeedContract,
    GivenContract,
    HistoryStoreContract,
    JobLockContract,
    SentBlocksContract,
    SettingsContract,
    SignInsContract,
    VaultStoreContract,
    _batch,
)

OTHER_KEY = b"\x08" * 32


class TestLocalJobLock(JobLockContract):
    @pytest.fixture
    def pair(self):
        lock = LocalJobLock()
        return lock, lock


class TestLocalVaultStore(VaultStoreContract):
    @pytest.fixture
    def stores(self, tmp_path):
        a, b = Store(tmp_path / "latet.db", ROOT_KEY), Store(tmp_path / "latet.db", ROOT_KEY)
        yield a, b
        a.close()
        b.close()

    @pytest.fixture
    def stranger(self, tmp_path, stores):
        other = Store(tmp_path / "latet.db", OTHER_KEY)
        yield other
        other.close()


class TestLocalHistoryStore(HistoryStoreContract):
    @pytest.fixture
    def stores(self, tmp_path):
        a = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
        b = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
        yield a, b
        a.close()
        b.close()


class TestLocalConversations(ConversationsContract):
    @pytest.fixture
    def pair(self):
        conversations = LocalConversations()
        return conversations, conversations

    def test_two_threads_making_the_same_new_conversation_get_one(self, monkeypatch):
        """The engine's thread and the bridge's, at the first request of a Claude Code session: what one shows the
        conversation, the other must restore."""
        together = threading.Barrier(2)

        def made_while_the_other_asks(key):
            with contextlib.suppress(threading.BrokenBarrierError):
                together.wait(0.2)  # both threads here: each found no conversation, and each would keep its own
            return Session(key)

        monkeypatch.setattr("latet.state.local.Session", made_while_the_other_asks)
        conversations, made = LocalConversations(), []
        threads = [threading.Thread(target=lambda: made.append(conversations.session("cc:new"))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert made[0] is made[1] and [kept is made[0] for kept in conversations.sessions()] == [True]


class TestLocalSentBlocks(SentBlocksContract):
    @pytest.fixture
    def pair(self):
        sent = LocalSentBlocks()
        return sent, sent

    def test_two_threads_asking_for_the_same_new_block_at_once_are_not_both_told_it_is_new(self, monkeypatch):
        """``first`` answers and counts the block as recorded in one step: a thread that asks while another is between
        the two does not get a second "new"."""
        sent, together = LocalSentBlocks(), threading.Barrier(2)
        read = sent._sent.get

        def read_while_the_other_reads(key):
            found = read(key)
            with contextlib.suppress(threading.BrokenBarrierError):
                together.wait(0.2)  # both threads here: each found no record, and each would call itself first
            return found

        monkeypatch.setattr(sent._sent, "get", read_while_the_other_reads)
        told = []
        threads = [threading.Thread(target=lambda: told.append(sent.first("conv-1", "b" * 32))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(told) == [False, True]


class TestLocalGiven(GivenContract):
    @pytest.fixture
    def pair(self):
        given = LocalGiven()
        return given, given


class TestLocalFeed(FeedContract):
    @pytest.fixture
    def pair(self):
        feed = Broadcaster()
        return feed, feed


class TestLocalApprovals(ApprovalsContract):
    @pytest.fixture
    def pair(self):
        queue = LocalApprovals()
        return queue, queue

    async def test_the_answer_is_handed_over_once_to_every_poll_waiting_for_it_and_its_call_is_forgotten(self):
        queue, batch = LocalApprovals(), _batch(polled=True)
        queue.put(batch)
        first = asyncio.ensure_future(queue.answer(batch.id, 5))
        second = asyncio.ensure_future(queue.answer(batch.id, 5))
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done() and batch.waiting == 2  # two polls held open
        queue.settle(batch.id, Decision(True))
        queue.give(batch.id, {"items": [], "degraded": []})
        assert await first == await second == {"items": [], "degraded": []}
        # The call, its batch and its original texts are forgotten as the answer leaves, not a minute later.
        assert queue._answers == {} and batch.waiting == 0
        with pytest.raises(KeyError):
            await queue.answer(batch.id, 0.05)

    async def test_an_answer_nobody_asks_for_any_more_is_forgotten_but_not_while_its_batch_waits(self, monkeypatch):
        queue, given, undecided = LocalApprovals(abandon_after=0.05), _batch(polled=True), _batch(polled=True)
        queue.put(given)
        queue.put(undecided)
        queue.settle(given.id, Decision(True))
        queue.give(given.id, {"items": [], "degraded": []})
        await asyncio.sleep(0.1)  # the plugin stopped asking (Esc, or closed)
        assert await queue.answer(undecided.id, 0.01) is None  # still waiting for you: asked again
        with pytest.raises(KeyError):
            await queue.answer(given.id, 0.01)
        assert list(queue._answers) == [undecided.id]

    async def test_a_call_that_is_never_given_its_answer_is_forgotten_once_nobody_asks_for_it(self, monkeypatch):
        """Whoever should give the answer may never do it (a task that failed first, was cancelled, or never started):
        the batch, its original texts and its images must not rest on that."""
        queue, silent, waiting = LocalApprovals(abandon_after=0.2), _batch(polled=True), _batch(polled=True)
        queue.put(silent)
        queue.put(waiting)
        queue.settle(silent.id, Decision(False, reason="the agent stopped waiting for it", state="abandoned"))
        queue.put(_batch(polled=True))
        assert silent.id in queue._answers  # asked about a moment ago: the plugin may be asking still
        await asyncio.sleep(0.3)  # nobody asked about it for longer than ABANDON_AFTER
        queue.put(_batch(polled=True))
        assert silent.id not in queue._answers and waiting.id in queue._answers  # the one that waits for you stays

    async def test_a_poll_held_open_on_a_decided_batch_keeps_its_slot_however_long_ago_it_began(self, monkeypatch):
        queue, batch = LocalApprovals(abandon_after=0.2), _batch(polled=True)
        queue.put(batch)
        queue.settle(batch.id, Decision(True))  # decided; its answer is not worked out yet
        asking = asyncio.ensure_future(queue.answer(batch.id, 5))
        await asyncio.sleep(0.3)  # the poll began longer ago than ABANDON_AFTER and is still open
        queue.put(_batch(polled=True))
        queue.give(batch.id, {"items": [], "degraded": []})
        assert await asyncio.wait_for(asking, 2) == {"items": [], "degraded": []}

    async def test_an_answer_nobody_asks_for_is_gone_a_while_after_the_decision_with_no_further_call(self, monkeypatch):
        """A decided batch's slot holds its original texts, its images and your decision. After a session's last call
        no other call comes to sweep it: it goes by itself once nobody asked about it for ABANDON_AFTER."""
        queue, batch = LocalApprovals(abandon_after=0.05), _batch(polled=True)
        queue.put(batch)
        queue.settle(batch.id, Decision(True))
        queue.give(batch.id, {"items": [], "degraded": []})
        assert list(queue._answers) == [batch.id]  # there, for the plugin's next poll
        await asyncio.sleep(1.3)  # ABANDON_AFTER and the second the sweep is set after it; nothing is called meanwhile
        assert queue._answers == {}

    async def test_a_slot_asked_about_once_more_is_gone_a_while_after_that_poll_ended(self, monkeypatch):
        """Whoever should give the answer may never do it: then the end of the plugin's last poll is the last call
        there is, and the slot goes ABANDON_AFTER after it."""
        queue, batch = LocalApprovals(abandon_after=0.05), _batch(polled=True)
        queue.put(batch)
        queue.settle(batch.id, Decision(True))  # decided, and nobody gives its answer
        assert await queue.answer(batch.id, 1.2) is None  # a poll held open past the sweep the decision set
        assert list(queue._answers) == [batch.id]  # asked about a moment ago: the plugin may ask again
        await asyncio.sleep(1.3)
        assert queue._answers == {}

    async def test_an_answer_nobody_asks_for_is_kept_the_time_the_backend_was_given(self, monkeypatch):
        """How long an answer nobody asks for is kept is the backend's own, given to it as a server's would be: never
        the gateway's, read from its module as the backend runs."""
        monkeypatch.setattr("latet.gateway.approvals.ABANDON_AFTER", 3600.0)  # the gateway's reaper: not read here
        queue, batch = LocalApprovals(abandon_after=0.05), _batch(polled=True)
        queue.put(batch)
        queue.settle(batch.id, Decision(True))
        queue.give(batch.id, {"items": [], "degraded": []})
        await asyncio.sleep(0.1)
        queue.put(_batch(polled=True))  # a call sweeps
        assert batch.id not in queue._answers
        assert LocalApprovals().abandon_after == 60.0  # as long as the gateway gives a batch nobody asks about

    def test_the_computers_approvals_import_nothing_of_the_gateways_approvals_as_they_run(self):
        tree = ast.parse(Path(local_module.__file__).read_text("utf-8"))
        imported = [
            (node.module, [alias.name for alias in node.names])
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not [
            (m, n)
            for m, n in imported
            if m == "latet.gateway.approvals" or (m, "approvals") == ("latet.gateway", *n[:1])
        ]


class TestLocalSignIns(SignInsContract):
    @pytest.fixture
    def clock(self):
        return Clock()

    @pytest.fixture
    def pair(self, clock):
        sign_ins = LocalSignIns(clock)
        return sign_ins, sign_ins

    def test_a_session_slides_twelve_hours_from_its_last_use(self, pair, clock):
        """On a computer each use starts its twelve hours again (the browser's cookie is set once, at sign-in, and ends
        twelve hours after it)."""
        sign_ins, _ = pair
        sid = sign_ins.open("")
        clock.now += SESSION_TTL - 1
        assert sign_ins.subject_of(sid) == ""
        clock.now += SESSION_TTL - 1  # twelve hours from its last use
        assert sign_ins.subject_of(sid) == ""
        clock.now += SESSION_TTL + 1
        assert sign_ins.subject_of(sid) is None

    def test_a_code_and_a_session_id_are_made_as_the_dashboard_always_made_them(self, pair):
        """24 random bytes for a code (32 characters), 32 for a session id (43)."""
        sign_ins, _ = pair
        codes, sids = {sign_ins.issue_code("") for _ in range(20)}, {sign_ins.open("") for _ in range(20)}
        assert (len(codes), len(sids)) == (20, 20)
        assert {len(code) for code in codes} == {32} and {len(sid) for sid in sids} == {43}

    def test_a_code_and_a_session_id_are_kept_only_as_their_hash(self, pair):
        """What is read from the memory opens no session: the SHA-256 of a secret is kept, never the secret."""
        sign_ins, _ = pair
        code, sid = sign_ins.issue_code("u_emilycarte"), sign_ins.open("u_emilycarte")
        [kept_code], [kept_sid] = sign_ins._codes, sign_ins._sessions
        assert kept_code == hashlib.sha256(code.encode()).hexdigest()
        assert kept_sid == hashlib.sha256(sid.encode()).hexdigest()
        assert code not in repr(vars(sign_ins)) and sid not in repr(vars(sign_ins))
        assert sign_ins.subject_of(kept_sid) is None and sign_ins.redeem(kept_code) is None  # the hash is no secret
        assert sign_ins.subject_of(sid) == "u_emilycarte" and sign_ins.redeem(code) == "u_emilycarte"

    def test_what_is_used_ended_or_late_is_gone_and_not_only_refused(self, pair, clock):
        sign_ins, _ = pair
        used, late, forgotten = (sign_ins.issue_code("u_emilycarte") for _ in range(3))
        assert sign_ins.redeem(used) == "u_emilycarte" and len(sign_ins._codes) == 2  # used: gone
        clock.now += CODE_TTL + 1
        assert sign_ins.redeem(late) is None and len(sign_ins._codes) == 1  # late: refused, and gone
        sign_ins.issue_code("u_emilycarte")  # the one nobody presented goes with the next code made
        assert len(sign_ins._codes) == 1 and sign_ins.redeem(forgotten) is None
        ended, expiring = sign_ins.open("u_emilycarte"), sign_ins.open("u_emilycarte")
        sign_ins.end(ended)
        assert len(sign_ins._sessions) == 1
        clock.now += SESSION_TTL + 1
        assert sign_ins.subject_of(expiring) is None and sign_ins._sessions == {}  # expired: refused, and gone

    def test_sessions_past_their_end_that_nobody_presents_are_gone_when_the_next_one_opens(self, pair, clock):
        """A session whose browser was closed is never asked about again, so nothing would drop it: it leaves the
        memory when the next session opens, as a code nobody presented does when the next code is made."""
        sign_ins, _ = pair
        first, second = sign_ins.open("u_emilycarte"), sign_ins.open("")
        clock.now += SESSION_TTL  # their end, to the second: still good
        third = sign_ins.open("u_emilycarte")
        assert len(sign_ins._sessions) == 3
        clock.now += 1  # past it
        fourth = sign_ins.open("")
        assert (
            len(sign_ins._sessions) == 2 and sign_ins.subject_of(first) is None and sign_ins.subject_of(second) is None
        )
        assert (sign_ins.subject_of(third), sign_ins.subject_of(fourth)) == ("u_emilycarte", "")


class TestLocalSettings(SettingsContract):
    @pytest.fixture
    def pair(self, tmp_path):
        return (
            LocalSettings(tmp_path / "config.yaml", tmp_path / "data"),
            LocalSettings(tmp_path / "config.yaml", tmp_path / "data"),
        )


class TestLocalAwsSessions(AwsSessionsContract):
    @pytest.fixture
    def clock(self):
        return Clock()

    @pytest.fixture
    def pair(self, clock):
        sessions = LocalAwsSessions(clock)
        return sessions, sessions

    def test_credentials_past_their_end_are_gone_from_the_memory_whether_asked_about_or_asked_for(self, pair, clock):
        """Past their end, credentials leave the memory when they are asked about as when they are asked for: the held
        connection of `latet claude` only asks whether its session is still here, and that must not keep them."""
        sessions, _ = pair
        about, asked_for = (
            sessions.add_session(AwsCredentials("AKIDEXAMPLE", "s", "t", expires=clock.now + 60)) for _ in range(2)
        )
        clock.now += 59
        assert sessions.has_session(about) and sessions.has_session(asked_for)  # a second before their end
        clock.now += 1  # their end
        assert not sessions.has_session(about) and about not in sessions._sessions
        with pytest.raises(AwsError):
            sessions.session(asked_for)
        assert sessions._sessions == {}
