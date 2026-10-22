"""What a Latet server keeps in Valkey for an identity: its conversations, the blocks they recorded, its live feeds, the
dashboard's sign-ins, its AWS sessions and its jobs' locks, held to the contracts a computer's memory meets (E0a), and
never a value, a text or a fake in clear.

Valkey is read and written by whoever has its password, without the master key: a record copied under another key, a
field written by hand, a mark copied from another conversation opens nothing and counts for nothing; a text masked on
one instance and decided on another leaves as decided; another identity sees nothing; and what one identity keeps has
ceilings, in Valkey and in an instance's memory."""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sys
import time

import pytest
import redis
from cryptography.exceptions import InvalidTag

from latet.config import Config, ProjectEntry, TermConfig
from latet.engine.build import build
from latet.engine.masker import ExtraValue, MaskReport
from latet.engine.pipeline import Session, personal_values
from latet.engine.types import EntityClass, Policy, Replacement, SlotKind
from latet.engine.vault import Vault
from latet.gateway.aws import AwsError
from latet.gateway.mediate import APPROVED_KEPT, Mediator
from latet.gateway.recorder import Recorder
from latet.gateway.routes import Upstream
from latet.gateway.sigv4 import AwsCredentials
from latet.projects import Projects, project_id
from latet.runtime import digest
from latet.server import live
from latet.server.live import (
    CONVERSATION_TTL,
    CONVERSATIONS_LARGEST,
    ENTITIES_KEPT,
    GIVEN_TTL,
    HELD_TTL,
    HERE_FOR,
    SpaceJobs,
    ValkeyAwsSessions,
    ValkeyConversations,
    ValkeyFeed,
    ValkeyGiven,
    ValkeySentBlocks,
    ValkeySignIns,
    _hash,
)
from latet.server.master import MasterKey
from latet.server.sealing import Sealer
from latet.state import CODE_TTL, SESSION_TTL, Left
from latet.storage.exchanges import ExchangeRecord, ExchangeStore

from ..conftest import ROOT_KEY
from ..unit.state.contracts import (
    AwsSessionsContract,
    ConversationsContract,
    FeedContract,
    GivenContract,
    JobLockContract,
    SentBlocksContract,
    SignInsContract,
)
from .conftest import deliver, valkey_dump

pytestmark = pytest.mark.server

MASTER = MasterKey([b"\x05" * 32])
OTHER_KEY = b"\x08" * 32  # another identity's root key
CANARY = "emily.carter@acme.com"
EMILY, DANIEL = "u_emilycarte", "u_danielokaf"
TEXT = "ask Emily Carter about the budget"
PERSON = ExtraValue("Emily Carter", "PERSON", EntityClass.CUSTOM, "human:person")  # your mask, in the Live Reviewer
REDACT = {"PERSON": "redact"}  # a person's name leaves as [REDACTED:PERSON], not as a fake
OSS, WORK = "/home/emily/oss-site", "/home/emily/billing-api"
OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")


@pytest.fixture(autouse=True)
def first_lines_said(monkeypatch):
    """Each test's own count of what does not open: the first line of each family is said at once."""
    monkeypatch.setattr(live, "UNREADABLE", live._Unreadable())


def _keys(bus, pattern: str) -> list[bytes]:
    """The keys of this test's prefix that match ``pattern`` (written after the prefix)."""
    return sorted(bus.client.scan_iter(match=bus.key(pattern), count=1000))


def _instance(vault, given, config=None, conversations=None):
    """An instance's mediator over the shared vault and its own view of Valkey, with the same rules."""
    config = config or Config()
    built = build(config, vault, None, conversations=conversations, given=given)
    built.engine.rules_tag = digest(config)
    built.engine.scopes = Projects(config, vault)
    return Mediator(built.engine, built.verifier, given=given)


def _mask(mediator, session="cc:s1", text=TEXT, **kwargs):
    return mediator.mask_value(text, SlotKind.USER_TEXT, mediator.engine.session(session), MaskReport(), **kwargs)


def _sent(mediator, text, session="cc:s1"):
    """The request that carries ``text`` leaves: the plugin's final check lets it out, and records it."""
    assert mediator.verifier.verify([(text, SlotKind.USER_TEXT, False)], session).blocking == []


def _engine(bus, vault, config, clock=time.time):
    """An instance's engine over the identity's conversations in Valkey, with the person's projects."""
    built = build(config, vault, None, conversations=ValkeyConversations(bus, EMILY, ROOT_KEY, clock=clock))
    built.engine.scopes = Projects(config, vault)
    return built.engine


@contextlib.asynccontextmanager
async def _ears(bus, *engines):
    """While it lasts, each message of the identity's channel is handed to the engines' conversations, as a server's ear
    hands it (Task 12): yields a wait until that many messages were heard in all."""
    heard = []

    class Ear:
        def heard(self, message):
            heard.append(message)
            for engine in engines:
                engine.conversations.heard(message)

    async def hear(count):
        for _ in range(100):
            if len(heard) >= count:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{len(heard)} messages heard, {count} expected")

    async with deliver(bus, f"identity:{EMILY}", Ear()):
        yield hear


class TestValkeyConversations(ConversationsContract):
    @pytest.fixture
    def pair(self, buses):
        a, b = buses
        return ValkeyConversations(a, EMILY, ROOT_KEY), ValkeyConversations(b, EMILY, ROOT_KEY)


class TestValkeySentBlocks(SentBlocksContract):
    @pytest.fixture
    def pair(self, buses):
        a, b = buses
        return ValkeySentBlocks(a, EMILY, ROOT_KEY), ValkeySentBlocks(b, EMILY, ROOT_KEY)


class TestValkeyGiven(GivenContract):
    @pytest.fixture
    def pair(self, buses):
        a, b = buses
        return ValkeyGiven(a, EMILY, ROOT_KEY), ValkeyGiven(b, EMILY, ROOT_KEY)


class TestValkeyFeed(FeedContract):
    @pytest.fixture
    async def pair(self, buses):
        a, b = buses
        mine, theirs = ValkeyFeed(a, f"feed:{EMILY}", ROOT_KEY), ValkeyFeed(b, f"feed:{EMILY}", ROOT_KEY)
        async with deliver(b, f"feed:{EMILY}", theirs):
            yield mine, theirs


class TestValkeySignIns(SignInsContract):
    @pytest.fixture
    def pair(self, buses, clock):
        a, b = buses
        return ValkeySignIns(a, MASTER, clock=clock), ValkeySignIns(b, MASTER, clock=clock)


class TestValkeyAwsSessions(AwsSessionsContract):
    @pytest.fixture
    def pair(self, buses, clock):
        a, b = buses
        return ValkeyAwsSessions(a, MASTER, EMILY, clock=clock), ValkeyAwsSessions(b, MASTER, EMILY, clock=clock)


class TestSpaceJobs(JobLockContract):
    @pytest.fixture
    def pair(self, buses):
        a, b = buses
        return SpaceJobs(a, EMILY), SpaceJobs(b, EMILY)


def test_a_conversation_tells_the_entities_another_instance_showed_it(buses):
    a, b = buses
    mine, theirs = ValkeyConversations(a, EMILY, ROOT_KEY), ValkeyConversations(b, EMILY, ROOT_KEY)
    told = []
    theirs.learn = told.append
    theirs.session("cc:s1")
    session = mine.session("cc:s1")
    session.add({"e1", "e2"})
    mine.update(session)
    theirs.session("cc:s1")
    theirs.session("cc:s1")
    assert told == [{"e1", "e2"}]  # once, when they are new here


def test_each_identitys_conversations_and_jobs_are_its_own(bus):
    mine, theirs = ValkeyConversations(bus, EMILY, ROOT_KEY), ValkeyConversations(bus, DANIEL, ROOT_KEY)
    session = mine.session("cc:shared-key")
    session.add({"e1"})
    mine.update(session)
    assert theirs.session("cc:shared-key").entity_ids == set() and theirs.sessions() == []
    with (
        SpaceJobs(bus, EMILY).hold("history-prune", 60) as one,
        SpaceJobs(bus, DANIEL).hold("history-prune", 60) as other,
    ):
        assert one and other


async def test_nothing_of_a_conversation_a_feed_or_a_session_is_in_valkey_in_clear(buses, prefix):
    """Every way of writing: no value, text or fake, and no conversation's key, response id, project id or folder, nor
    the plain SHA-256 of one of them (which a guess would find)."""
    a, b = buses
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    session = conversations.session("cc:session-1")
    session.add({"e1"})
    session.project_id, session.cwd = project_id(WORK), WORK
    conversations.update(session)
    conversations.remember(b"R" * 32, f"You said: write to {CANARY}")
    conversations.name("resp_canary-1", "cc:session-1")
    assert conversations.flush(5)  # written off the caller's thread: in Valkey now
    ValkeyAwsSessions(a, MASTER, EMILY).add_session(AwsCredentials("AKIDEXAMPLE", CANARY))
    replaced = Replacement(9, 30, CANARY, "EMAIL_1", "EMAIL", EntityClass.PII, "pii:email", Policy.MASK, "e" + "1" * 19)
    given = ValkeyGiven(a, EMILY, ROOT_KEY)
    given.keep(b"g" * 32, Left("write to EMAIL_1 about billing-api", (replaced,), True))
    given.keep_checked("cc:session-1", [b"g" * 32])
    assert ValkeySentBlocks(a, EMILY, ROOT_KEY).first("cc:session-1", "b" * 32)
    ValkeySignIns(a, MASTER).open(EMILY)
    ValkeySignIns(a, MASTER).issue_code(EMILY)
    published = []

    class Ear:
        def heard(self, message):
            published.append(message)

    with SpaceJobs(a, EMILY).hold("history-prune", 60) as held:
        assert held
        async with deliver(b, f"feed:{EMILY}", Ear()):
            ValkeyFeed(a, f"feed:{EMILY}", ROOT_KEY).publish({"id": "ex_1", "block_reason": CANARY})
            for _ in range(100):
                if published:
                    break
                await asyncio.sleep(0.05)
        seen = valkey_dump(a, prefix) + repr(published).encode()
    assert published and conversations.recall(b"R" * 32) == f"You said: write to {CANARY}"
    for clear in (CANARY, "/home/emily", "billing-api"):
        assert clear.encode() not in seen
    for named in ("cc:session-1", "resp_canary-1", project_id(WORK), WORK):
        assert named.encode() not in seen
        assert hashlib.sha256(named.encode()).hexdigest()[:16].encode() not in seen


def test_a_sign_in_record_written_by_hand_or_sealed_by_another_server_opens_no_dashboard(buses, clock):
    a, b = buses
    sid = ValkeySignIns(a, MASTER, clock=clock).open(EMILY)
    forged = "a-session-id-the-writer-chose"
    record = json.dumps({"s": EMILY, "e": clock.now + 3600})
    a.client.set(a.key(f"signin:session:{_hash(forged)}"), record)  # as someone with Valkey's password would
    assert ValkeySignIns(b, MASTER, clock=clock).subject_of(forged) is None  # not sealed by this server
    other = ValkeySignIns(b, MasterKey([b"\x06" * 32]), clock=clock)
    assert other.subject_of(sid) is None  # another master key: not its session
    assert ValkeySignIns(b, MASTER, clock=clock).subject_of(sid) == EMILY  # and its look ended nothing
    moved = a.client.get(a.key(f"signin:session:{_hash(sid)}"))
    a.client.set(a.key(f"signin:session:{_hash(forged)}"), moved)  # a sealed record copied under another key
    assert ValkeySignIns(b, MASTER, clock=clock).subject_of(forged) is None  # bound to its own key


def test_a_person_keeps_their_newest_sessions_and_the_older_end_on_every_instance(buses, clock, monkeypatch):
    """B3: a person keeps their newest ``SESSIONS_KEPT`` dashboard sessions, as an identity keeps its newest AWS
    sessions: one more ends the oldest, in Valkey and in every instance's memory (told on ``signins``), and leaves
    their one-time codes and another person's sessions as they are. Ten sign-in links a minute for twelve hours kept
    7,200 of them, which used in turn took some 1,400 lookups a second."""
    a, b = buses
    mine, theirs = ValkeySignIns(a, MASTER, clock=clock), ValkeySignIns(b, MASTER, clock=clock)
    told, publish = [], a.publish

    def telling(channel, message):
        told.append((channel, message))
        return publish(channel, message)

    monkeypatch.setattr(a, "publish", telling)
    code = mine.issue_code(EMILY)
    sessions = []
    for _ in range(live.SESSIONS_KEPT):
        sessions.append(mine.open(EMILY))
        clock.now += 1
    other = mine.open(DANIEL)  # another person's are their own
    assert [theirs.subject_of(sid) for sid in sessions] == [EMILY] * live.SESSIONS_KEPT and told == []
    assert mine.subject_of(sessions[0]) == EMILY and mine.held(sessions[0]) == EMILY  # held in this one's memory
    newest = mine.open(EMILY)
    assert theirs.subject_of(sessions[0]) is None  # the oldest ended
    assert [theirs.subject_of(sid) for sid in [*sessions[1:], newest]] == [EMILY] * live.SESSIONS_KEPT
    assert mine.held(sessions[0]) is None and told == [("signins", {"session": _hash(sessions[0])})]
    assert theirs.subject_of(other) == DANIEL and theirs.redeemed(code) == (EMILY, None)  # the code stays


def test_a_session_found_open_is_held_a_few_seconds_but_not_one_read_while_an_end_was_heard(buses, clock, monkeypatch):
    """A3: an instance holds a session it found open for ``SESSION_HELD`` seconds (``held``, what the gate answers on the
    event loop), and drops it when it hears its end; a read that began before an end heard meanwhile answers what it
    read and keeps nothing, so that the memory never serves a session ended elsewhere."""
    a, _ = buses
    monkeypatch.setattr(live, "SESSION_HELD", 0.3)
    sign_ins = ValkeySignIns(a, MASTER, clock=clock)
    sid = sign_ins.open(EMILY)
    assert sign_ins.held(sid) is None  # opened, never read here
    assert sign_ins.subject_of(sid) == EMILY and sign_ins.held(sid) == EMILY
    time.sleep(0.35)
    assert sign_ins.held(sid) is None  # its few seconds over: read again
    assert sign_ins.subject_of(sid) == EMILY and sign_ins.held(sid) == EMILY
    sign_ins.heard({"session": _hash(sid)})  # signed out on another instance
    assert sign_ins.held(sid) is None
    real = a.client.get

    def ended_meanwhile(key):
        found = real(key)
        sign_ins.heard({"subject": EMILY})  # her every session ended elsewhere, heard while this read was made
        return found

    monkeypatch.setattr(a.client, "get", ended_meanwhile)
    assert sign_ins.subject_of(sid) == EMILY  # what it read, before the end
    assert sign_ins.held(sid) is None  # and kept nothing


# --- a conversation's project and folder, and a response's conversation (C1) --------------------------------------


def test_a_conversations_project_moved_or_written_by_hand_turns_latet_off_nowhere(buses):
    """Latet is off in one folder of the person's; a conversation works in another. Its project and folder copied
    from the off folder's conversation, and a project id written by hand beside them: masked on both instances, also
    once the plugin reports its folder again."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=OSS, enabled=False)])
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, config), _engine(b, vault, config)
    oss, work = here.session("cc:oss"), here.session("cc:work")
    here.attach(oss, OSS)
    here.attach(work, WORK)
    text = f"write to {CANARY}"
    assert here.mask_text(text, SlotKind.USER_TEXT, oss).text == text  # Latet is off there
    conversations = here.conversations
    off = a.client.hget(conversations._conv(conversations._name("cc:oss")), "w")
    a.client.hset(conversations._conv(conversations._name("cc:work")), mapping={"w": off, "p": project_id(OSS)})
    seen = there.session("cc:work")
    assert (seen.project_id, seen.cwd) == (None, None)  # bound to its own conversation: it does not open here
    for engine in (here, there):
        session = engine.session("cc:work")
        assert CANARY not in engine.mask_text(text, SlotKind.USER_TEXT, session).text
        engine.attach(session, WORK)  # the plugin reports the folder at its next call
        assert CANARY not in engine.mask_text(text, SlotKind.USER_TEXT, session).text


def test_a_conversation_moved_to_another_folder_and_back_is_back_on_every_instance(buses):
    """Moved on one instance, from one folder to another and back: another instance, which holds the conversation
    already, follows each move."""
    a, b = buses
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, Config()), _engine(b, vault, Config())
    session = here.session("cc:moving")
    for folder in (WORK, OSS, WORK):
        here.attach(session, folder)
        seen = there.session("cc:moving")
        assert (seen.project_id, seen.cwd) == (project_id(folder), folder)


def test_a_response_written_by_hand_or_copied_from_another_names_no_conversation(buses):
    a, b = buses
    mine, theirs = ValkeyConversations(a, EMILY, ROOT_KEY), ValkeyConversations(b, EMILY, ROOT_KEY)
    mine.name("resp_1", "cc:one")
    mine.name("resp_2", "cc:two")
    assert mine.flush(5)
    first, second = mine._response_key("resp_1"), mine._response_key("resp_2")
    a.client.set(first, a.client.get(second))  # copied from another response
    assert theirs.named("resp_1") is None and theirs.named("resp_2") == "cc:two"
    a.client.set(first, "cc:the-writers-choice")  # written by hand
    assert theirs.named("resp_1") is None


# --- a text as it left, on two instances (C2, from Task 9a) -------------------------------------------------------


@pytest.mark.parametrize("policies", [{}, REDACT])
def test_a_text_masked_but_never_sent_does_not_leave_once_decided_on_another_instance(buses, policies):
    """The text waits for you on one instance (masked with the name in clear: not known yet); you mask the name, and
    another instance sends it masked. The conversation's next request comes back to the first instance, whose memory
    in front of Valkey still holds the text as it was before your decision: it is masked anew, never sent as it never
    left. The same where the name's type is redacted."""
    a, b = buses
    config, vault = Config(policies=policies), Vault(ROOT_KEY)
    here = _instance(vault, ValkeyGiven(a, EMILY, ROOT_KEY), config)
    there = _instance(vault, ValkeyGiven(b, EMILY, ROOT_KEY), config)
    assert _mask(here).text == TEXT  # held for your decision, and kept as masked so far
    decided = _mask(there, extra=[PERSON])
    assert "Emily Carter" not in decided.text
    _sent(there, decided.text)
    assert _mask(here).text == decided.text


@pytest.mark.parametrize("policies", [{}, REDACT])
def test_a_record_written_back_as_it_was_before_your_decision_does_not_leave(buses, policies):
    """Someone with Valkey's password saves a text's first record, then writes it back once your decision replaced
    it: neither the instance that decided nor a fresh one sends the name."""
    a, b = buses
    config, vault = Config(policies=policies), Vault(ROOT_KEY)
    here = _instance(vault, ValkeyGiven(a, EMILY, ROOT_KEY), config)
    assert _mask(here).text == TEXT  # held for your decision
    [record] = _keys(a, f"given:{EMILY}:*")
    before = a.client.get(record)
    decided = _mask(here, extra=[PERSON])
    _sent(here, decided.text)
    assert _mask(here).text == decided.text  # the next request: as it left
    a.client.set(record, before)  # written back by hand
    assert "Emily Carter" not in _mask(here).text
    fresh = _instance(vault, ValkeyGiven(b, EMILY, ROOT_KEY), config)
    assert "Emily Carter" not in _mask(fresh).text


# --- the entities a conversation was shown (I1) ---------------------------------------------------------------------


def test_an_entity_written_by_hand_or_copied_from_another_conversation_is_not_taken(buses):
    """Not in the conversation another instance reads, not handed to ``learn``, and its fake not restored in that
    conversation's answers; where it was shown, it is."""
    a, b = buses
    vault = Vault(ROOT_KEY)
    entity = vault.intern("EMAIL", EntityClass.PII, CANARY)
    mine = ValkeyConversations(a, EMILY, ROOT_KEY)
    other = mine.session("cc:other")
    other.add({entity.id})
    mine.update(other)
    work = mine.session("cc:work")
    work.add({"e" + "1" * 19})
    mine.update(work)
    target = mine._conv(mine._name("cc:work"))
    copied = a.client.hget(mine._conv(mine._name("cc:other")), f"e:{entity.id}")
    a.client.hset(target, f"e:{entity.id}", copied)  # with its tag, from another conversation of the identity
    a.client.hset(target, "e:" + "2" * 20, "")  # by hand
    built = build(Config(), vault, None, conversations=ValkeyConversations(b, EMILY, ROOT_KEY))
    told = []
    built.engine.conversations.learn = told.append
    seen = built.engine.session("cc:work")
    assert seen.entity_ids == {"e" + "1" * 19} and told == [{"e" + "1" * 19}]
    assert [r.entity_id for r in built.engine.restores(seen, json_mode=False)] == []
    shown = built.engine.session("cc:other")
    assert entity.id in {r.entity_id for r in built.engine.restores(shown, json_mode=False)}


# --- ceilings (I2, R-10) ---------------------------------------------------------------------------------------------


def test_an_identitys_conversations_have_a_ceiling_and_the_least_recently_used_go_whole(buses, clock, monkeypatch):
    a, b = buses
    monkeypatch.setattr(live, "CONVERSATIONS_KEPT", 3)
    mine = ValkeyConversations(a, EMILY, ROOT_KEY, clock=clock)

    def used(n):
        clock.now += 1
        session = mine.session(f"cc:s{n}")
        session.add({f"e{n:019d}"})
        session.approved.add(f"{n:064x}")
        mine.update(session)

    for n in range(5):
        used(n)
    assert a.client.zcard(a.key(f"convs:{EMILY}")) == 3
    assert len(_keys(a, f"conv:{EMILY}:*")) == 3 and len(_keys(a, f"approved:{EMILY}:*")) == 3
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY, clock=clock)
    assert sorted(s.key for s in theirs.sessions()) == ["cc:s2", "cc:s3", "cc:s4"]
    assert (theirs.session("cc:s0").entity_ids, theirs.session("cc:s0").approved) == (set(), set())  # gone whole
    clock.now += 1
    mine.session("cc:s2")  # read: used now
    used(5)
    assert sorted(s.key for s in theirs.sessions()) == ["cc:s2", "cc:s4", "cc:s5"]


def test_a_conversation_keeps_its_latest_approved_texts_in_valkey_and_in_a_session_read_elsewhere(buses, clock):
    a, b = buses
    mine = ValkeyConversations(a, EMILY, ROOT_KEY, clock=clock)
    session = mine.session("cc:s1")
    for batch in range(5):  # 2,500 approvals, 500 at a time
        clock.now += 1
        session.approved.update(f"{batch * 500 + n:064x}" for n in range(500))
        mine.update(session)
    assert a.client.zcard(mine._approved(mine._name("cc:s1"))) == APPROVED_KEPT == 2_000
    seen = ValkeyConversations(b, EMILY, ROOT_KEY).session("cc:s1")
    assert seen.approved == {f"{n:064x}" for n in range(500, 2_500)}  # the latest


def test_texts_as_they_left_have_a_ceiling_and_the_oldest_go_with_their_records(buses, clock, monkeypatch):
    a, b = buses
    monkeypatch.setattr(live, "GIVEN_KEPT", 3)
    mine = ValkeyGiven(a, EMILY, ROOT_KEY, clock=clock)
    for n in range(5):
        clock.now += 1
        mine.keep(bytes([n]) * 32, Left(f"text {n}", (), True))
    assert a.client.zcard(a.key(f"givens:{EMILY}")) == 3 and len(_keys(a, f"given:{EMILY}:*")) == 3
    theirs = ValkeyGiven(b, EMILY, ROOT_KEY, clock=clock)
    kept = [Left(f"text {n}", (), True) for n in (2, 3, 4)]
    assert [theirs.masked(bytes([n]) * 32) for n in range(5)] == [None, None, *kept]


def test_a_text_larger_than_the_limit_is_not_kept_and_is_masked_anew_when_sent_again(buses, monkeypatch):
    """Both left, the vault learnt the name since: the text that was kept leaves as it left, the one too large to keep
    is masked anew, on the instance that masked it as on another."""
    a, b = buses
    monkeypatch.setattr(live, "GIVEN_LARGEST", 300)
    vault = Vault(ROOT_KEY)
    here = _instance(vault, ValkeyGiven(a, EMILY, ROOT_KEY))
    large = TEXT + " and the figures of each quarter" * 10  # sealed, over 300 bytes
    for text in (TEXT, large):
        assert _mask(here, text=text).text == text  # the name not known yet
        _sent(here, text)
    assert len(_keys(a, f"given:{EMILY}:*")) == 1  # the small one alone
    vault.intern("PERSON", EntityClass.CUSTOM, "Emily Carter")
    there = _instance(vault, ValkeyGiven(b, EMILY, ROOT_KEY))
    for instance in (here, there):
        assert _mask(instance, text=TEXT).text == TEXT  # as it left
        assert "Emily Carter" not in _mask(instance, text=large).text


def test_the_blocks_that_left_and_those_recorded_have_a_ceiling_each(buses, clock, monkeypatch):
    a, b = buses
    monkeypatch.setattr(live, "CHECKED_KEPT", 3)
    monkeypatch.setattr(live, "SENT_KEPT", 3)
    given, sent = ValkeyGiven(a, EMILY, ROOT_KEY, clock=clock), ValkeySentBlocks(a, EMILY, ROOT_KEY, clock=clock)
    for n in range(5):
        clock.now += 1
        given.keep_checked(f"cc:s{n}", [bytes([n]) * 32])
        assert sent.first(f"cc:s{n}", "b" * 32)
    assert a.client.zcard(a.key(f"checked:{EMILY}")) == 3 and a.client.zcard(a.key(f"sent:{EMILY}")) == 3
    theirs = ValkeyGiven(b, EMILY, ROOT_KEY, clock=clock)
    assert [theirs.checked(f"cc:s{n}", bytes([n]) * 32) for n in range(5)] == [False, False, True, True, True]
    recorded = ValkeySentBlocks(b, EMILY, ROOT_KEY, clock=clock)
    assert recorded.first("cc:s0", "b" * 32) and not recorded.first("cc:s4", "b" * 32)  # the oldest is new once more


def test_the_texts_of_the_model_and_the_response_ids_have_a_ceiling_each(buses, clock, monkeypatch):
    a, b = buses
    monkeypatch.setattr(live, "REVERSE_KEPT", 3)
    monkeypatch.setattr(live, "RESPONSES_KEPT", 3)
    mine = ValkeyConversations(a, EMILY, ROOT_KEY, clock=clock)
    for n in range(5):
        clock.now += 1
        mine.remember(bytes([n]) * 32, f"You said: {n}")
        mine.name(f"resp_{n}", f"cc:s{n}")
    assert mine.flush(5)
    for family in ("reverse", "response"):
        assert a.client.zcard(a.key(f"{family}s:{EMILY}")) == 3 and len(_keys(a, f"{family}:{EMILY}:*")) == 3
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY, clock=clock)
    assert [theirs.recall(bytes([n]) * 32) for n in range(5)] == [
        None,
        None,
        "You said: 2",
        "You said: 3",
        "You said: 4",
    ]
    assert [theirs.named(f"resp_{n}") for n in range(5)] == [None, None, "cc:s2", "cc:s3", "cc:s4"]


def test_an_identitys_aws_sessions_have_a_ceiling(buses, clock, monkeypatch):
    a, b = buses
    monkeypatch.setattr(live, "AWS_SESSIONS_KEPT", 3)
    mine = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock)
    sessions = []
    for n in range(5):
        clock.now += 1
        sessions.append(mine.add_session(AwsCredentials(f"AKID{n}", "s")))
    theirs = ValkeyAwsSessions(b, MASTER, EMILY, clock=clock)
    assert [theirs.has_session(session) for session in sessions] == [False, False, True, True, True]
    assert len(_keys(a, f"aws:{EMILY}:*")) == 3


def test_what_ended_leaves_a_subjects_list_of_sign_ins_as_the_next_is_listed(buses, clock):
    a, _ = buses
    sign_ins = ValkeySignIns(a, MASTER, clock=clock)
    sign_ins.issue_code(EMILY)
    clock.now += CODE_TTL + 1  # the code ended, unused
    sid = sign_ins.open(EMILY)
    listed = a.client.zrange(a.key(sign_ins.index(EMILY)), 0, -1)
    assert listed == [a.key(f"signin:session:{_hash(sid)}").encode()]


def test_every_aws_session_of_an_identity_is_dropped_at_once_with_no_scan_of_valkey(buses, clock, monkeypatch):
    a, b = buses
    mine, others = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock), ValkeyAwsSessions(a, MASTER, DANIEL, clock=clock)
    sessions = [mine.add_session(AwsCredentials("AKIDEXAMPLE", f"s{n}")) for n in range(3)]
    kept = others.add_session(AwsCredentials("AKIDOTHER", "s"))

    def scanned(*args, **kwargs):
        raise AssertionError("Valkey's keys were scanned")

    monkeypatch.setattr(b.client, "scan_iter", scanned)
    monkeypatch.setattr(b.client, "scan", scanned)
    assert ValkeyAwsSessions(b, MASTER, EMILY, clock=clock).drop_all() == 3
    assert not any(mine.has_session(session) for session in sessions) and others.has_session(kept)
    assert _keys(a, f"aws:{EMILY}:*") == [] and _keys(a, f"aws-sessions:{EMILY}") == []
    assert ValkeyAwsSessions(b, MASTER, EMILY, clock=clock).drop_all() == 0


# --- an instance's memories (I10) ------------------------------------------------------------------------------------


def _taken_by(kept) -> int:
    """What a conversation an instance holds takes, as Python counts it: its objects, its sets and their strings (a
    string two sets hold counts once)."""
    session = kept.session
    sets = (session.entity_ids, session.approved, kept.entities, kept.approved)
    strings = {id(item): item for found in sets for item in found}
    texts = (session.key, session.cwd, session.project_id)
    return (
        sys.getsizeof(session)
        + sys.getsizeof(kept)
        + sys.getsizeof(vars(kept))
        + sum(sys.getsizeof(found) for found in sets)
        + sum(sys.getsizeof(item) for item in strings.values())
        + sum(sys.getsizeof(text) for text in texts if text)
    )


def test_the_memories_in_front_of_valkey_are_bounded_in_bytes_and_say_what_they_hold(buses, monkeypatch):
    a, _ = buses
    roomy = ValkeyConversations(a, EMILY, ROOT_KEY)  # a server's own bound: one large conversation fits
    large = roomy.session("cc:large")
    large.add({f"e{m:019x}" for m in range(1_000)})
    large.approved.update(f"{m:064x}" for m in range(500))
    roomy.update(large)
    [kept] = roomy._here.values()
    assert live._kept_weight(kept) >= _taken_by(kept)  # what it says it holds is no less than what it holds
    monkeypatch.setattr(live, "GIVEN_HERE_BYTES", 64 * 1024)
    monkeypatch.setattr(live, "CONVERSATIONS_HERE_BYTES", 256 * 1024)
    given, conversations = ValkeyGiven(a, EMILY, ROOT_KEY), ValkeyConversations(a, EMILY, ROOT_KEY)
    assert given.cached_bytes == conversations.cached_bytes == 0
    for n in range(40):
        given.keep(n.to_bytes(32, "big"), Left(f"{n} " + "x" * 4_000, (), True))
    texts = given.cached_bytes
    assert 32 * 1024 < texts <= 64 * 1024
    given.keep_checked("cc:s1", [n.to_bytes(32, "big") for n in range(1_000)])
    memory = given._checked._data
    taken = sys.getsizeof(memory) + sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in memory.items())
    assert given.cached_bytes - texts == 1_000 * live._CHECKED_BYTES >= taken
    for n in range(20):
        session = conversations.session(f"cc:s{n}")
        session.add({f"e{n:03d}{m:016x}" for m in range(100)})
        session.approved.update(f"{n:032x}{m:032x}" for m in range(50))
        conversations.update(session)
    held = conversations._here.weight
    assert 128 * 1024 < held <= 256 * 1024
    # And a note of each conversation, kept apart (a heavy conversation has one too): each no more than it says.
    notes = conversations._notes._data

    def own(note):  # what a note holds of its own: its digests, its versions and its count, each object once
        return {id(part): part for part in (note.where, note.known, note.version, note.heard, note.fields) if part}

    taken = sys.getsizeof(notes) + sum(
        sys.getsizeof(name) + sys.getsizeof(note) + sum(map(sys.getsizeof, own(note).values()))
        for name, note in notes.items()
    )
    assert conversations.cached_bytes - held == 20 * live._NOTE_BYTES >= taken


def test_what_the_memory_in_front_holds_is_asked_of_valkey_again_after_a_day_and_kept_alive_there(buses, clock):
    a, _ = buses
    given = ValkeyGiven(a, EMILY, ROOT_KEY, clock=clock)
    given.keep(b"g" * 32, Left("write to EMAIL_1", (), True))
    given.keep_checked("cc:s1", [b"1" * 32])
    record, blocks = a.key(f"given:{EMILY}:{(b'g' * 32).hex()}"), a.key(f"checked:{EMILY}")
    a.client.expire(record, 100)
    [(member, first)] = a.client.zrange(blocks, 0, -1, withscores=True)
    clock.now += HERE_FOR - 1
    assert given.masked(b"g" * 32) is not None and given.checked("cc:s1", b"1" * 32)
    assert a.client.ttl(record) <= 100 and a.client.zscore(blocks, member) == first  # from memory: nothing asked
    clock.now += 2
    assert given.masked(b"g" * 32) is not None and given.checked("cc:s1", b"1" * 32)
    assert a.client.ttl(record) > CONVERSATION_TTL - 60 and a.client.zscore(blocks, member) == clock.now


# --- the dashboard's sign-ins (R-5, M2) -----------------------------------------------------------------------------


def test_a_dashboard_session_ends_twelve_hours_after_it_was_opened_whatever_its_use(buses, clock):
    a, b = buses
    mine, theirs = ValkeySignIns(a, MASTER, clock=clock), ValkeySignIns(b, MASTER, clock=clock)
    sid = mine.open(EMILY)
    for _ in range(11):
        clock.now += 3600
        assert theirs.subject_of(sid) == EMILY  # used every hour
    clock.now += 3600
    assert theirs.subject_of(sid) == EMILY  # twelve hours from its opening, to the second
    clock.now += 1
    assert theirs.subject_of(sid) is None


def test_a_sign_in_record_written_back_opens_within_its_own_limits_and_no_longer(buses, clock):
    """Someone with Valkey's password saves a session's record, and writes it back once the subject's sessions were
    ended: it opens again (the dashboard's check of the identity is what holds then), within its twelve hours only."""
    a, b = buses
    mine, theirs = ValkeySignIns(a, MASTER, clock=clock), ValkeySignIns(b, MASTER, clock=clock)
    sid = mine.open(EMILY)
    key = a.key(f"signin:session:{_hash(sid)}")
    saved = a.client.get(key)
    theirs.end(subject=EMILY)
    assert theirs.subject_of(sid) is None
    a.client.set(key, saved)  # written back by hand
    assert theirs.subject_of(sid) == EMILY
    clock.now += SESSION_TTL + 1
    assert theirs.subject_of(sid) is None


def test_a_look_writes_nothing_so_a_session_ended_meanwhile_stays_ended(buses, clock, monkeypatch):
    a, b = buses
    mine, theirs = ValkeySignIns(a, MASTER, clock=clock), ValkeySignIns(b, MASTER, clock=clock)
    sid, other = mine.open(EMILY), mine.open(EMILY)
    key = a.key(f"signin:session:{_hash(sid)}")
    lifetime = a.client.pttl(a.key(f"signin:session:{_hash(other)}"))
    clock.now += 3600
    assert theirs.subject_of(other) == EMILY
    assert a.client.pttl(a.key(f"signin:session:{_hash(other)}")) <= lifetime  # a look renews nothing
    read = b.client.get

    def read_then_ended(name):
        found = read(name)
        mine.end(sid)  # ended between the look's read and what a look would write after it
        return found

    with monkeypatch.context() as patched:
        patched.setattr(b.client, "get", read_then_ended)
        assert theirs.subject_of(sid) == EMILY  # what it read
    assert theirs.subject_of(sid) is None and not a.client.exists(key)  # nothing wrote it back


def test_ending_a_subject_ends_a_session_used_to_its_last_minute(buses, clock):
    a, b = buses
    mine, theirs = ValkeySignIns(a, MASTER, clock=clock), ValkeySignIns(b, MASTER, clock=clock)
    sid = mine.open(EMILY)
    index, key = a.key(mine.index(EMILY)), a.key(f"signin:session:{_hash(sid)}")
    for _ in range(11):
        clock.now += 3600
        assert theirs.subject_of(sid) == EMILY
    clock.now += 3600 - 60
    assert a.client.ttl(index) >= a.client.ttl(key) > 0  # the list outlives every record it lists
    theirs.end(subject=EMILY)
    assert mine.subject_of(sid) is None and not a.client.exists(key)


def test_sign_ins_keep_hashes_of_their_secrets_only_under_names_of_one_form(buses, prefix, clock):
    a, _ = buses
    sign_ins = ValkeySignIns(a, MASTER, clock=clock)
    codes = [sign_ins.issue_code(EMILY) for _ in range(3)]
    sids = [sign_ins.open(EMILY) for _ in range(3)] + [sign_ins.open(DANIEL)]
    assert {len(code) for code in codes} == {32} and {len(sid) for sid in sids} == {43}
    form = re.compile(re.escape(prefix) + r"signin:(code|session|subject):[0-9a-f]{32}")
    keys = [key.decode() for key in a.client.scan_iter(match=prefix + "*")]
    assert len(keys) == 9 and all(form.fullmatch(key) for key in keys)
    seen = valkey_dump(a, prefix)
    assert not any(secret.encode() in seen for secret in codes + sids)
    # S7 (area 6, M3): a subject's list is named by a keyed hash, never the plain hash of an id of 50 bits.
    for subject in (EMILY, DANIEL):
        assert subject.encode() not in seen and _hash(subject).encode() not in seen


# --- AWS sessions (R-22, M3) ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("expires", [float("inf"), float("nan"), 1e300, "a year ahead"])
def test_an_aws_sessions_end_that_is_not_a_number_or_too_far_counts_as_none(buses, clock, expires):
    a, b = buses
    end = clock.now + 365 * 86400 if expires == "a year ahead" else expires
    session = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock).add_session(AwsCredentials("AKIDEX", "s", "t", end))
    assert 0 < a.client.ttl(a.key(f"aws:{EMILY}:{_hash(session)}")) <= HELD_TTL + 60
    theirs = ValkeyAwsSessions(b, MASTER, EMILY, clock=clock)
    assert theirs.session(session) == AwsCredentials("AKIDEX", "s", "t", None)
    clock.now += HELD_TTL + 1
    assert not theirs.has_session(session)  # unused for an hour


def test_an_aws_session_is_kept_an_hour_after_its_last_use_and_never_past_its_end(buses, clock):
    a, b = buses
    mine, theirs = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock), ValkeyAwsSessions(b, MASTER, EMILY, clock=clock)
    day = mine.add_session(AwsCredentials("AKIDEXAMPLE", "s", "t", clock.now + 86400))
    soon = mine.add_session(AwsCredentials("AKIDEXAMPLE", "s", "t", clock.now + 600))
    assert a.client.ttl(a.key(f"aws:{EMILY}:{_hash(soon)}")) <= 600 + 60  # never past its end
    clock.now += HELD_TTL - 1
    assert theirs.has_session(day) and not theirs.has_session(soon)  # used: an hour from now, again
    clock.now += HELD_TTL - 1
    assert theirs.has_session(day)
    clock.now += HELD_TTL + 1
    assert not theirs.has_session(day)  # unused for an hour and a second, a day before its end


def test_an_aws_session_is_sealed_for_its_purpose_and_key_and_its_id_is_in_no_key(buses, prefix, clock):
    a, b = buses
    mine = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock)
    session, other = mine.add_session(AwsCredentials("AKIDEXAMPLE", CANARY)), mine.add_session(AwsCredentials("A", "b"))
    key = a.key(f"aws:{EMILY}:{_hash(session)}")
    raw = a.client.get(key)
    assert json.loads(MASTER.sealer(b"aws-sessions").open(raw, key))["s"] == CANARY
    with pytest.raises(InvalidTag):
        MASTER.open(raw, key)  # not what the master key seals for itself
    assert not any(found.encode() in valkey_dump(a, prefix) for found in (session, other, CANARY))
    a.client.set(key, a.client.get(a.key(f"aws:{EMILY}:{_hash(other)}")))  # moved from another session's key
    with pytest.raises(AwsError) as gone:
        ValkeyAwsSessions(b, MASTER, EMILY, clock=clock).session(session)
    assert gone.value.code == "aws-session-gone"
    assert ValkeyAwsSessions(b, MASTER, EMILY, clock=clock).has_session(other)


# --- what does not open (R-1, M4) -------------------------------------------------------------------------------------


def test_a_record_copied_under_another_key_does_not_open(buses):
    """A text as it left, what the model wrote, a conversation's key: each opens under its own key alone (a text as not
    left, nothing recalled, the conversation not listed under another's key). A project and folder, and a response's
    conversation, are tried above."""
    a, b = buses
    given, conversations = ValkeyGiven(a, EMILY, ROOT_KEY), ValkeyConversations(a, EMILY, ROOT_KEY)
    for n in (1, 2):
        given.keep(bytes([n]) * 32, Left(f"write to EMAIL_{n}", (), True))
        conversations.remember(bytes([n]) * 32, f"You said: {n}")
        session = conversations.session(f"cc:{n}")
        session.add({"e1"})
        conversations.update(session)
    assert conversations.flush(5)
    a.client.set(given._record((b"\x02" * 32).hex()), a.client.get(given._record((b"\x01" * 32).hex())))
    a.client.set(
        conversations._reverse((b"\x02" * 32).hex()), a.client.get(conversations._reverse((b"\x01" * 32).hex()))
    )
    first, second = (conversations._conv(conversations._name(f"cc:{n}")) for n in (1, 2))
    a.client.hset(second, "k", a.client.hget(first, "k"))
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    assert ValkeyGiven(b, EMILY, ROOT_KEY).masked(b"\x02" * 32) is None
    assert theirs.recall(b"\x02" * 32) is None and theirs.recall(b"\x01" * 32) == "You said: 1"
    assert [session.key for session in theirs.sessions()] == ["cc:1"]


async def test_what_does_not_open_counts_as_absent_and_is_said_once_for_its_family(buses, clock, caplog):
    a, b = buses
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    session = conversations.session("cc:s1")
    session.project_id, session.cwd = "p1", WORK
    session.approved.add("ab12")
    conversations.update(session)
    name = conversations._name("cc:s1")
    junk = {"w": b"junk", b"\xff\xfe": b"x", "e:" + "3" * 20: b"junk"}  # a project, a field's name, an entity's tag
    a.client.hset(conversations._conv(name), mapping=junk)
    a.client.zadd(conversations._approved(name), {b"\xff": clock.now})
    conversations.remember(b"R" * 32, "You said: hi")
    conversations.name("resp_1", "cc:s1")
    assert conversations.flush(5)
    a.client.set(conversations._reverse((b"R" * 32).hex()), b"junk")
    a.client.set(conversations._response_key("resp_1"), b"\xff")
    given = ValkeyGiven(a, EMILY, ROOT_KEY)
    given.keep(b"g" * 32, Left("write to EMAIL_1", (), True))
    a.client.set(given._record((b"g" * 32).hex()), b"junk")
    a.client.set(a.key(f"signin:session:{_hash('sid')}"), b"junk")
    aws = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock)
    held = aws.add_session(AwsCredentials("AKIDEXAMPLE", "s"))
    a.client.set(a.key(f"aws:{EMILY}:{_hash(held)}"), b"junk")

    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    seen = theirs.session("cc:s1")
    assert (seen.project_id, seen.cwd, seen.entity_ids, seen.approved) == (None, None, set(), {"ab12"})
    assert theirs.recall(b"R" * 32) is None and theirs.named("resp_1") is None
    assert ValkeyGiven(b, EMILY, ROOT_KEY).masked(b"g" * 32) is None
    assert ValkeySignIns(b, MASTER, clock=clock).subject_of("sid") is None
    with pytest.raises(AwsError):
        ValkeyAwsSessions(b, MASTER, EMILY, clock=clock).session(held)
    feed = ValkeyFeed(b, f"feed:{EMILY}", ROOT_KEY)
    queue = feed.subscribe()
    for message in ({"feed": "not base64!"}, {"feed": base64.b64encode(b"junk").decode()}, {"feed": 5}):
        feed.heard(message)
    assert queue.empty()
    lines = [record.getMessage() for record in caplog.records if record.name == "latet.server.live"]
    for family in ("conv", "approved", "reverse", "response", "given", "signin", "aws", "feed"):
        assert sum(f"Valkey's {family} family" in line for line in lines) == 1, family
    assert live.UNREADABLE.counts["conv"] == 3 and live.UNREADABLE.counts["feed"] == 3


# --- a conversation kept while it is used (M5) ------------------------------------------------------------------------


def test_a_conversation_read_is_kept_a_week_from_then(buses):
    a, b = buses
    mine = ValkeyConversations(a, EMILY, ROOT_KEY)
    session = mine.session("cc:s1")
    session.add({"e1"})
    session.approved.add("ab12")
    mine.update(session)
    name = mine._name("cc:s1")
    for key in (mine._conv(name), mine._approved(name)):
        a.client.expire(key, 100)
    ValkeyConversations(b, EMILY, ROOT_KEY).session("cc:s1")
    for key in (mine._conv(name), mine._approved(name)):
        assert a.client.ttl(key) > CONVERSATION_TTL - 60


@pytest.mark.parametrize(
    "lost", ["the whole conversation", "an entity", "its key", "its project", "its approved texts"]
)
def test_a_conversation_valkey_lost_is_whole_again_on_another_instance_after_the_next_update(buses, lost):
    a, b = buses
    mine = ValkeyConversations(a, EMILY, ROOT_KEY)
    session = mine.session("cc:s1")
    session.add({"e1", "e2"})
    session.project_id, session.cwd = "p1", WORK
    session.approved.add("ab12")
    mine.update(session)
    name = mine._name("cc:s1")
    fields = {"an entity": "e:e1", "its key": "k", "its project": "w"}
    if lost in fields:
        a.client.hdel(mine._conv(name), fields[lost])
    elif lost == "its approved texts":
        a.client.delete(mine._approved(name))
    else:
        a.client.delete(mine._conv(name), mine._approved(name))
    session = mine.session("cc:s1")  # the next request: what is gone is seen gone
    session.add({"e3"})
    mine.update(session)
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    assert [listed.key for listed in theirs.sessions()] == ["cc:s1"]  # its key is there again
    seen = theirs.session("cc:s1")
    assert (seen.entity_ids, seen.project_id, seen.cwd, seen.approved) == ({"e1", "e2", "e3"}, "p1", WORK, {"ab12"})


# --- a text as it left keeps no value (M6) ----------------------------------------------------------------------------


def test_a_text_as_it_left_is_kept_without_its_values_and_counted_as_before(buses, prefix):
    a, b = buses
    phone = "+39 333 1234567"
    replacements = (
        Replacement(9, 30, CANARY, "[REDACTED:EMAIL]", "EMAIL", EntityClass.PII, "pii:email", Policy.REDACT, None),
        Replacement(
            35, 47, "Emily Carter", "[REDACTED:PERSON]", "PERSON", EntityClass.NER, "ner:person", Policy.REDACT, None
        ),
        Replacement(52, 73, CANARY, "[REDACTED:EMAIL]", "EMAIL", EntityClass.PII, "pii:email", Policy.REDACT, None),
        Replacement(
            78, 93, phone, "+39 351 7654321", "PHONE", EntityClass.PII, "pii:phone", Policy.MASK, "e" + "4" * 19
        ),
    )
    text = "write to [REDACTED:EMAIL] and [REDACTED:PERSON] at [REDACTED:EMAIL] or +39 351 7654321"
    left = Left(text, replacements, False)
    ValkeyGiven(a, EMILY, ROOT_KEY).keep(b"g" * 32, left)
    member = (b"g" * 32).hex()
    opened = Sealer(ROOT_KEY, b"valkey").open(a.client.get(a.key(f"given:{EMILY}:{member}")), f"{EMILY}:given:{member}")
    seen = valkey_dump(a, prefix) + opened
    for value in (CANARY, "Emily Carter", phone):
        assert value.encode() not in seen and hashlib.sha256(value.encode()).hexdigest()[:16].encode() not in seen
    back = ValkeyGiven(b, EMILY, ROOT_KEY).masked(b"g" * 32)
    assert back is not None and (back.text, back.exact) == (text, False)
    assert personal_values(back.replacements) == personal_values(replacements) == 3


# --- a job's lock held for ever (M8) ------------------------------------------------------------------------------------


def test_with_its_lock_held_for_ever_a_history_is_pruned_of_what_is_a_day_past_its_retention(bus, tmp_path, caplog):
    """The lock held by hand, with no end: a recorder made a moment ago (a space built again, an instance started again)
    prunes at its first try what is more than a day past the retention; what is less waits for the lock's holder, and is
    pruned under the lock once it is free."""
    lock = bus.key(f"lock:history-prune:{EMILY}")
    bus.client.set(lock, "by-hand")  # as someone with Valkey's password could
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    store.retention_days = 14
    now = time.time()
    for days in (16, 14.5, 3):
        store.record(ExchangeRecord({"ts": now - days * 86400, "session": f"{days}d", "agent": "codex", "flags": []}))
    recorder = Recorder(store, jobs=SpaceJobs(bus, EMILY))
    try:
        recorder.prune_now()
        assert recorder.drain() and [s["session"] for s in store.list()[0]] == ["3d", "14.5d"]
        assert "pruned without it" in caplog.text
        bus.client.delete(lock)
        recorder.prune_now()
        assert recorder.drain() and [s["session"] for s in store.list()[0]] == ["3d"]
    finally:
        recorder.close()
        store.close()


# --- what the final check let leave (R-11) ----------------------------------------------------------------------------


def test_a_block_marked_as_left_by_hand_or_for_another_conversation_has_not_left(buses):
    a, b = buses
    one = b"1" * 32
    ValkeyGiven(a, EMILY, ROOT_KEY).keep_checked("cc:s1", [one])
    a.client.zadd(a.key(f"checked:{EMILY}"), {one.hex(): 1, hashlib.sha256(b"cc:s2" + one).hexdigest(): 1})
    theirs = ValkeyGiven(b, EMILY, ROOT_KEY)
    assert theirs.checked("cc:s1", one) and not theirs.checked("cc:s2", one)


def test_a_second_check_of_the_same_request_asks_valkey_nothing(buses, monkeypatch):
    a, _ = buses
    built = build(Config(), Vault(ROOT_KEY), None, given=ValkeyGiven(a, EMILY, ROOT_KEY))
    slots = [(f"note {n}: the build is green", SlotKind.USER_TEXT, False) for n in range(50)]
    assert built.verifier.verify(slots, "cc:s1").leaks == []
    asked = []
    command, pipeline = a.client.execute_command, a.client.pipeline
    monkeypatch.setattr(a.client, "execute_command", lambda *args, **kw: asked.append(args[0]) or command(*args, **kw))
    monkeypatch.setattr(a.client, "pipeline", lambda *args, **kw: asked.append("pipeline") or pipeline(*args, **kw))
    assert built.verifier.verify(slots, "cc:s1").leaks == []
    assert asked == []


# --- another identity (R-26) ------------------------------------------------------------------------------------------


def test_another_identity_sees_nothing_of_an_identitys_conversations_texts_blocks_and_sessions(buses, clock):
    a, b = buses
    conversations, given = ValkeyConversations(a, EMILY, ROOT_KEY), ValkeyGiven(a, EMILY, ROOT_KEY)
    conversations.name("resp_1", "cc:s1")
    conversations.remember(b"R" * 32, "You said: hi")
    assert conversations.flush(5)
    given.keep(b"k" * 32, Left("write to EMAIL_1", (), True))
    given.keep_checked("cc:s1", [b"1" * 32])
    assert ValkeySentBlocks(a, EMILY, ROOT_KEY).first("cc:s1", "b" * 32)
    aws = ValkeyAwsSessions(a, MASTER, EMILY, clock=clock)
    session = aws.add_session(AwsCredentials("AKIDEXAMPLE", "s"))
    for key in (OTHER_KEY, ROOT_KEY):  # Daniel's own root key, as on a server; or Emily's: the identity alone
        theirs, others = ValkeyConversations(b, DANIEL, key), ValkeyGiven(b, DANIEL, key)
        assert theirs.named("resp_1") is None and theirs.recall(b"R" * 32) is None
        assert others.masked(b"k" * 32) is None and not others.checked("cc:s1", b"1" * 32)
        assert ValkeySentBlocks(b, DANIEL, key).first("cc:s1", "b" * 32)
    with pytest.raises(AwsError):
        ValkeyAwsSessions(b, MASTER, DANIEL, clock=clock).session(session)
    assert ValkeyAwsSessions(b, MASTER, DANIEL, clock=clock).drop_all() == 0 and aws.has_session(session)


async def test_an_event_too_large_for_a_message_goes_by_reference_within_its_identitys_budget(
    buses, prefix, monkeypatch, caplog
):
    """Task 10's Important 1: every message on the bus stays small. A larger event is kept a minute under a key of its
    own, sealed for it, and the message names it; what one identity keeps so has a budget, past which an event reaches
    no viewer (as a stalled viewer misses events), said in the log."""
    monkeypatch.setattr(live, "EVENTS_BYTES", 350_000)
    a, b = buses
    channel = f"approvals:{EMILY}"
    mine, theirs = ValkeyFeed(a, channel, ROOT_KEY), ValkeyFeed(b, channel, ROOT_KEY)
    viewer = theirs.subscribe()
    guidance = "CANARY-GUIDANCE " * 10_000  # a change's long guidance: 160 kB
    spy = a.client.pubsub()
    spy.subscribe(a.key(channel))
    assert spy.get_message(timeout=5)["type"] == "subscribe"
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    try:
        async with deliver(b, channel, theirs):
            for n in (1, 2, 3):
                mine.publish({"id": f"change-{n}", "guidance": guidance}, "change")
            mine.publish({"id": "change-4"}, "change")
            heard = [await asyncio.wait_for(viewer.get(), 5) for _ in range(3)]
        wires = []
        while (message := spy.get_message(timeout=1)) is not None:
            if message["type"] == "message":
                wires.append(message["data"])
    finally:
        spy.close()
    assert [(event, payload["id"]) for event, payload in heard] == [
        ("change", "change-1"), ("change", "change-2"), ("change", "change-4")
    ]  # fmt: skip
    assert heard[0][1]["guidance"] == guidance and viewer.empty()
    assert len(wires) == 3 and max(len(wire) for wire in wires) < 1024  # the third reached no viewer, and no wire
    kept = [key for key in a.client.scan_iter(match=a.key(f"event:{EMILY}:*"), count=1000)]
    assert len(kept) == 2 and sum(a.client.strlen(key) for key in kept) <= 350_000
    assert b"CANARY-GUIDANCE" not in valkey_dump(a, prefix)
    [line] = [record.getMessage() for record in caplog.records if record.name == "latet.server.live"]
    assert EMILY in line and "CANARY" not in line


def test_a_publish_valkey_fails_is_said_in_the_log_and_raises_nothing(bus, monkeypatch, caplog):
    feed = ValkeyFeed(bus, f"feed:{EMILY}", ROOT_KEY)

    def fails(*args, **kwargs):
        raise redis.ConnectionError("Error 10061 connecting to valkey.internal:6379.")

    monkeypatch.setattr(bus.client, "publish", fails)
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    feed.publish({"id": "ex_1"})  # the request that made it goes on: viewers miss this event
    feed.publish({"id": "ex_2"})
    [line] = [record.getMessage() for record in caplog.records if record.name == "latet.server.live"]
    assert "ConnectionError" in line and "valkey.internal" not in line


async def test_a_feeds_message_never_reaches_another_identitys_viewers(buses):
    """Not on its own channel; and not even handed its ``heard`` by mistake: it does not open there."""
    a, b = buses
    emily, daniel = ValkeyFeed(a, f"feed:{EMILY}", ROOT_KEY), ValkeyFeed(b, f"feed:{DANIEL}", OTHER_KEY)
    queue, heard = daniel.subscribe(), []

    class Misrouted:
        def heard(self, message):
            heard.append(message)
            daniel.heard(message)

    async with deliver(b, f"feed:{EMILY}", Misrouted()), deliver(b, f"feed:{DANIEL}", daniel):
        emily.publish({"id": "ex_1"})
        for _ in range(100):
            if heard:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
    assert heard and queue.empty()


# --- what an identity's texts as they left take, in bytes (fix round 1, Important 1) ------------------------------------


SIZES = (
    0, 1, 10, 252, 253, 1_000, 4_090, 4_091, 10_000, 65_528, 65_529, 65_536, 100_000, 1_000_000, 4 * 1024 * 1024 + 1,
    5_000_000,
)  # fmt: skip


def test_what_the_byte_budgets_count_of_a_value_bounds_what_valkey_takes_for_it(bus):
    """Valkey's allocator rounds a value up to a size class, up to a quarter more (56 MiB of values counted were 70 MiB
    held), and a key takes a few hundred bytes beside it: ``weighed`` counts both, never less than ``MEMORY USAGE`` says
    and at most a key's few hundred bytes more; the scripts count alike."""
    for size in SIZES:
        key = bus.key(f"weighed:{size}")
        bus.client.set(key, os.urandom(size), px=60_000)
        used = bus.client.memory_usage(key, samples=0)
        assert used <= live.weighed(size) <= used + live.ENTRY_BYTES, (size, used, live.weighed(size))
    script = bus.client.register_script(live.WEIGHED_LUA + "return weighed(tonumber(ARGV[1]))")
    for size in (*SIZES, 7, 16, 17, 31, 32, 33, 2**20 - 17, 2**20 - 16, 2**20, 123_456_789):
        assert int(script(args=[size])) == live.weighed(size), size


async def test_a_feed_without_the_queue_says_that_a_pending_event_reached_no_viewer(bus, caplog):
    """A feed the queue was not given to (left unwired, or another version's) cannot read a batch a "pending" names:
    the event reaches no viewer, said in the log once a minute, never silently."""
    feed = ValkeyFeed(bus, f"approvals:{EMILY}", ROOT_KEY)
    viewer = feed.subscribe()
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    feed.heard({"pending": "b_0123456789ab"})
    feed.heard({"pending": "b_ba9876543210"})
    assert viewer.empty()
    [line] = [record.getMessage() for record in caplog.records if record.name == "latet.server.live"]
    assert "pending" in line and EMILY in line and "b_0123456789ab" not in line


def _member(n: int) -> str:
    """The name in Valkey of the text kept under the key ``bytes([n]) * 32``."""
    return (bytes([n]) * 32).hex()


def _given_bytes(bus) -> tuple[int, int]:
    """What the identity's texts as they left take, as counted beside them, and as Valkey takes their records (each
    record's allocation and its key, ``live.weighed``)."""
    total = int(bus.client.hget(bus.key(f"givens-size:{EMILY}"), "#total") or 0)
    members = [member.decode() for member in bus.client.zrange(bus.key(f"givens:{EMILY}"), 0, -1)]
    return total, sum(live.weighed(bus.client.strlen(bus.key(f"given:{EMILY}:{member}"))) for member in members)


def test_texts_as_they_left_take_a_budget_of_bytes_and_the_least_recently_used_go(buses, clock, monkeypatch):
    """Two instances keep; past the budget the text least recently kept or read goes with its record. The count is
    exact: kept beside the texts in the step that keeps one, and what a record deleted by hand took is taken off when
    its turn to go comes."""
    a, b = buses
    monkeypatch.setattr(live, "GIVEN_BYTES", 5_200, raising=False)
    mine, theirs = ValkeyGiven(a, EMILY, ROOT_KEY, clock=clock), ValkeyGiven(b, EMILY, ROOT_KEY, clock=clock)

    def kept(n, view):
        clock.now += 1
        view.keep(bytes([n]) * 32, Left(f"{n} " + "x" * 900, (), True))  # 1,280 bytes as Valkey takes it: four fit

    def members():
        return sorted(member.decode() for member in a.client.zrange(a.key(f"givens:{EMILY}"), 0, -1))

    for n in range(4):
        kept(n, mine if n % 2 else theirs)
    clock.now += 1
    assert ValkeyGiven(b, EMILY, ROOT_KEY, clock=clock).masked(bytes([0]) * 32) is not None  # read: used now
    kept(4, mine)
    assert members() == sorted(_member(n) for n in (0, 2, 3, 4))  # the least recently used went
    total, taken = _given_bytes(a)
    assert total == taken <= 5_200
    a.client.delete(a.key(f"given:{EMILY}:{_member(2)}"))  # by hand: counted until its turn to go comes
    kept(5, theirs)
    assert members() == sorted(_member(n) for n in (0, 3, 4, 5))
    total, taken = _given_bytes(a)
    assert total == taken <= 5_200


def test_a_text_unused_for_a_week_is_gone_and_counted_no_more_though_valkey_would_keep_it_a_day_longer(buses, clock):
    a, b = buses
    mine = ValkeyGiven(a, EMILY, ROOT_KEY, clock=clock)
    mine.keep(bytes([1]) * 32, Left("write to EMAIL_1", (), True))
    record = a.key(f"given:{EMILY}:{_member(1)}")
    assert a.client.ttl(record) > GIVEN_TTL  # Valkey's own lifetime is a day longer: a backstop
    clock.now += GIVEN_TTL + 1
    assert ValkeyGiven(b, EMILY, ROOT_KEY, clock=clock).masked(bytes([1]) * 32) is None  # a week unused: gone
    mine.keep(bytes([2]) * 32, Left("write to EMAIL_2", (), True))  # the next text kept takes it out, and its count
    assert not a.client.exists(record)
    assert _given_bytes(a) == (live.weighed(a.client.strlen(a.key(f"given:{EMILY}:{_member(2)}"))),) * 2


# --- what an identity's conversations hold (fix round 1, Important 2) ----------------------------------------------------


def test_a_conversation_keeps_no_more_entities_in_valkey_than_its_ceiling(buses, monkeypatch):
    """Those past it are restored only where the conversation is held: the safe side."""
    a, b = buses
    monkeypatch.setattr(live, "ENTITIES_KEPT", 6, raising=False)  # its key, its project and four entities
    mine = ValkeyConversations(a, EMILY, ROOT_KEY)
    session = mine.session("cc:s1")
    session.project_id, session.cwd = "p1", WORK
    session.add({f"e{n:019d}" for n in range(10)})
    mine.update(session)
    conv = mine._conv(mine._name("cc:s1"))
    assert a.client.hlen(conv) == 6
    sent, write = [], mine._write

    def counted(keys, args):
        sent.append(int(args[11]))  # the entities sent to be written
        return write(keys=keys, args=args)

    mine._write = counted
    session.add({"e" + "9" * 19})
    mine.update(session, shown={"e" + "9" * 19})  # full when last written: nothing is sent, nor written
    assert a.client.hlen(conv) == 6 and sent == []
    seen = ValkeyConversations(b, EMILY, ROOT_KEY).session("cc:s1")
    assert len(seen.entity_ids) == 4 and seen.entity_ids < session.entity_ids
    assert len(mine.session("cc:s1").entity_ids) == 11  # where it is held, every one


def _filled(conversations, key: str, short: int = 0) -> None:
    """A conversation at the real ceiling (``short`` fields under it): its key, its project and its entities."""
    session = conversations.session(key)
    session.project_id, session.cwd = "p1", WORK
    session.add({f"e{n:019d}" for n in range(ENTITIES_KEPT - 2 - short)})
    conversations.update(session)
    assert conversations.bus.client.hlen(conversations._conv(conversations._name(key))) == ENTITIES_KEPT - short


@pytest.mark.parametrize("short", [0, 1], ids=["full", "one-field-short"])
def test_at_its_ceiling_a_conversation_is_written_once_at_most_however_many_new_values_a_request_names(buses, short):
    """The real ceiling. One request of fifty texts, each naming an address the conversation was never shown, through
    another instance: a full conversation is written to no more, one a field short of it once, with what that first
    text showed; and the instance holds the conversation, which restores every one of them."""
    a, b = buses
    _filled(ValkeyConversations(a, EMILY, ROOT_KEY), "cc:full", short)
    conversations = ValkeyConversations(b, EMILY, ROOT_KEY)
    calls, write = [], conversations._write

    def counted(keys, args):
        calls.append((int(args[10]), int(args[11]), int(args[12])))  # fields, entities, approved texts sent
        return write(keys=keys, args=args)

    conversations._write = counted
    mediator = _instance(Vault(ROOT_KEY), ValkeyGiven(b, EMILY, ROOT_KEY), conversations=conversations)
    session = mediator.engine.session("cc:full")  # one request
    for n in range(50):
        mediator.mask_value(f"write to person{n}@acme-{n}.com", SlotKind.USER_TEXT, session, MaskReport())
    assert len(calls) == short and all(fields == texts == 0 and 0 < shown <= 2 for fields, shown, texts in calls)
    assert a.client.hlen(conversations._conv(conversations._name("cc:full"))) == ENTITIES_KEPT
    assert len(session.entity_ids) >= ENTITIES_KEPT - 2 - short + 50
    assert "cc:full" in {kept.session.key for kept in conversations._here.values()}


def _script_calls_on(bus, key: str, action) -> list[str]:
    """The commands a script made on ``key`` while ``action`` ran, as Valkey's ``MONITOR`` shows them: other clients'
    commands, this test's own included, are left out."""
    token = "done:" + secrets.token_hex(8)
    found: list[str] = []
    with bus.client.monitor() as monitor:
        action()
        bus.client.echo(token)
        while token not in (command := monitor.next_command())["command"]:
            name, *rest = command["command"].split(" ", 2)
            if command["client_type"] == "lua" and rest[:1] == [key]:
                found.append(name.upper())
    return found


def test_an_update_reads_a_conversations_size_once_and_writes_its_fields_256_to_a_call(buses):
    """What the script makes Valkey do: a conversation filled to its ceiling in one update costs a call for each 256
    entities, and fifty more sent by an instance that read it before (its note of it out of date) cost none of their
    own, there being no room for them; the conversation's size is read once each time."""
    a, b = buses
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    session = theirs.session("cc:full")  # read while empty
    conv = theirs._conv(theirs._name("cc:full"))
    filling = _script_calls_on(a, conv, lambda: _filled(ValkeyConversations(a, EMILY, ROOT_KEY), "cc:full"))
    assert filling.count("HLEN") == 1 and len(filling) <= 3 + math.ceil((ENTITIES_KEPT - 2) / 256)
    session.add({f"n{n:019d}" for n in range(50)})
    late = _script_calls_on(b, conv, lambda: theirs.update(session))
    assert late.count("HLEN") == 1 and len(late) <= 3
    assert b.client.hlen(conv) == ENTITIES_KEPT


def test_a_conversation_an_instance_does_not_hold_is_sent_only_what_each_call_shows_it(buses, monkeypatch):
    """One heavier than an instance holds of one is read whole at each use: each value a request names after that is
    sent alone (``Engine.show`` says which), not every entity of the conversation again; another instance restores
    them all."""
    a, b = buses
    monkeypatch.setattr(live, "CONVERSATIONS_LARGEST", 64 * 1024, raising=False)
    engine = _engine(a, Vault(ROOT_KEY), Config())
    conversations = engine.conversations
    engine.show(engine.session("cc:heavy"), {f"h{m:019d}" for m in range(1_000)})
    assert "cc:heavy" not in {kept.session.key for kept in conversations._here.values()}  # too heavy to be held
    sent, write = [], conversations._write

    def counted(keys, args):
        sent.append(int(args[11]))  # the entities sent to be written
        return write(keys=keys, args=args)

    conversations._write = counted
    for n in range(3):
        engine.show(engine.session("cc:heavy"), {f"n{n:019d}"})  # a request each, naming a new value
    assert sent == [1, 1, 1]
    assert len(ValkeyConversations(b, EMILY, ROOT_KEY).session("cc:heavy").entity_ids) == 1_003


def test_a_conversation_at_its_ceiling_weighs_no_more_than_an_instance_holds_of_one():
    """Its entities, and as many approved texts as a conversation keeps, each marked as written, with a folder as long
    as a client may report: within what an instance holds of one conversation, so that a full one is held."""
    session = Session("cc:" + "k" * 64, project_id="p" * 12, cwd="/" * 4096)
    session.add({f"e{n:019x}" for n in range(ENTITIES_KEPT)})
    session.approved.update(f"{n:064x}" for n in range(APPROVED_KEPT))
    kept = live._Kept(session, entities=set(session.entity_ids), approved=set(session.approved))
    assert live._kept_weight(kept) <= CONVERSATIONS_LARGEST


def test_an_identitys_conversations_hold_a_budget_of_entries_and_the_least_recently_used_go_whole(
    buses, clock, monkeypatch
):
    """Counted exactly, beside the index of conversations: a conversation deleted by hand is taken off when its turn to
    go comes."""
    a, b = buses
    monkeypatch.setattr(live, "CONVERSATION_ENTRIES", 12, raising=False)
    mine = ValkeyConversations(a, EMILY, ROOT_KEY, clock=clock)

    def used(n):
        clock.now += 1
        session = mine.session(f"cc:s{n}")
        session.add({f"e{n:02d}{m:017d}" for m in range(3)})  # with its key: four entries
        mine.update(session)

    def held():
        names = [name.decode() for name in a.client.zrange(a.key(f"convs:{EMILY}"), 0, -1)]
        entries = sum(a.client.hlen(mine._conv(name)) + a.client.zcard(mine._approved(name)) for name in names)
        return int(a.client.hget(a.key(f"convs-size:{EMILY}"), "#total") or 0), entries

    for n in range(3):
        used(n)
    assert held() == (12, 12)
    used(3)
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY, clock=clock)
    assert sorted(s.key for s in theirs.sessions()) == ["cc:s1", "cc:s2", "cc:s3"] and held() == (12, 12)
    assert theirs.session("cc:s0").entity_ids == set()  # gone whole
    a.client.delete(mine._conv(mine._name("cc:s1")))  # by hand: counted until its turn to go comes
    used(4)
    assert sorted(s.key for s in theirs.sessions()) == ["cc:s2", "cc:s3", "cc:s4"] and held() == (12, 12)


def test_a_heavy_conversation_is_not_held_by_an_instance_rather_than_flushing_the_others(buses, monkeypatch):
    a, _ = buses
    monkeypatch.setattr(live, "CONVERSATIONS_HERE_BYTES", 256 * 1024)
    monkeypatch.setattr(live, "CONVERSATIONS_LARGEST", 64 * 1024, raising=False)
    mine = ValkeyConversations(a, EMILY, ROOT_KEY)
    for n in range(3):
        light = mine.session(f"cc:light{n}")
        light.add({f"e{n:019d}"})
        mine.update(light)
    heavy = mine.session("cc:heavy")
    heavy.add({f"h{m:019d}" for m in range(1_000)})
    mine.update(heavy)
    assert {kept.session.key for kept in mine._here.values()} == {"cc:light0", "cc:light1", "cc:light2"}
    assert mine.session("cc:heavy").entity_ids == heavy.entity_ids  # not held: read whole from Valkey


# --- a conversation's project on another instance (fix round 1, Important 3 and 4) --------------------------------------


def test_a_conversations_project_applies_on_an_instance_that_never_resolved_its_folder(buses):
    """The plugin's first call of a conversation lands on one instance, which resolves its folder; the next on
    another, which never did: the project's term is masked there through the proxy's path (no folder reported) and
    after the plugin's ``attach``."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=WORK, terms=[TermConfig(value="Project Falcon", type="CODENAME")])])
    vault = Vault(ROOT_KEY)
    here = _instance(vault, ValkeyGiven(a, EMILY, ROOT_KEY), config, ValkeyConversations(a, EMILY, ROOT_KEY))
    there = _instance(vault, ValkeyGiven(b, EMILY, ROOT_KEY), config, ValkeyConversations(b, EMILY, ROOT_KEY))
    here.engine.attach(here.engine.session("cc:s1"), WORK)
    assert _mask(here, text="set up the repository").text == "set up the repository"
    text = "the Project Falcon launch is on Friday"
    assert "Project Falcon" not in _mask(there, text=text).text
    there.engine.attach(there.engine.session("cc:s1"), WORK)
    assert "Project Falcon" not in _mask(there, text=f"{text}, again").text


def test_a_project_written_back_from_the_conversations_past_turns_latet_off_nowhere(buses):
    """A conversation begins where Latet is off, its project and folder saved by someone with Valkey's password, then
    moves to the work folder through both instances; written back, the saved one is older than what each instance
    holds: it does not open for them, and the address stays masked on both, the proxy's next request included."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=OSS, enabled=False)])
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, config), _engine(b, vault, config)
    conv = here.conversations._conv(here.conversations._name("cc:x"))
    session = here.session("cc:x")
    here.attach(session, OSS)
    saved = a.client.hget(conv, "w")
    here.attach(session, WORK)
    text = f"write to {CANARY}"
    assert CANARY not in here.mask_text(text, SlotKind.USER_TEXT, here.session("cc:x")).text
    there.attach(there.session("cc:x"), OSS)
    there.attach(there.session("cc:x"), WORK)
    assert CANARY not in there.mask_text(text, SlotKind.USER_TEXT, there.session("cc:x")).text
    a.client.hset(conv, "w", saved)  # written back by hand
    for engine in (here, there):
        for _ in range(2):  # the proxy's next requests (Claude Code's: they report no folder)
            seen = engine.session("cc:x")
            assert seen.cwd == WORK and CANARY not in engine.mask_text(text, SlotKind.USER_TEXT, seen).text
    assert live.UNREADABLE.counts.get("conv") == 4


def test_latet_off_from_valkey_alone_applies_on_an_instance_only_once_a_client_reports_the_folder_there(buses):
    """Latet off in a folder of the person's, a conversation there through one instance. Another instance takes the
    conversation's project from Valkey, but no client reported that folder to it for this conversation: it keeps
    masking, until the plugin's call reports the folder there."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=OSS, enabled=False)])
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, config), _engine(b, vault, config)
    here.attach(here.session("cc:oss"), OSS)
    text = f"write to {CANARY}"
    assert here.mask_text(text, SlotKind.USER_TEXT, here.session("cc:oss")).text == text  # reported there: off
    seen = there.session("cc:oss")
    assert (seen.project_id, seen.cwd) == (project_id(OSS), OSS)  # as Valkey says
    assert CANARY not in there.mask_text(text, SlotKind.USER_TEXT, seen).text
    there.attach(seen, OSS)  # the plugin's call to this instance reports the folder
    assert there.mask_text(text, SlotKind.USER_TEXT, seen).text == text


def test_an_agent_that_writes_its_folder_in_its_requests_reports_it_to_each_instance(buses):
    """OpenCode writes its working directory in its system prompt: a request of the conversation that does reports the
    folder to the instance it reaches, and Latet is off there for it; one that does not leaves the instance masking."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=OSS, enabled=False)])
    vault = Vault(ROOT_KEY)
    here = _instance(vault, ValkeyGiven(a, EMILY, ROOT_KEY), config, ValkeyConversations(a, EMILY, ROOT_KEY))
    there = _instance(vault, ValkeyGiven(b, EMILY, ROOT_KEY), config, ValkeyConversations(b, EMILY, ROOT_KEY))

    def request(mediator, folder=None):
        system = f"Here is some useful information:\n<env>\n  Working directory: {folder}\n</env>" if folder else "hi"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": f"write to {CANARY}"}]
        body = json.dumps({"model": "m", "messages": messages}).encode()
        return mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", {"session-id": "s1"}, body)

    assert request(here, OSS).off
    assert not request(there).off  # the conversation's project from Valkey alone
    assert request(there, OSS).off


@pytest.mark.parametrize("undone", ["written back", "deleted"])
async def test_a_holder_that_hears_of_a_later_move_forgets_its_clients_report(buses, undone):
    """Latet off in a folder of the person's, reported to one instance for a conversation (that instance hears its own
    word of it back, and forgets nothing for it); the conversation moves to the work folder through another, which says
    so on the identity's channel. Valkey's writer then puts back the record of the off folder, or deletes the record,
    before the first instance reads it: that instance, which heard of a later version, forgot its client's report and
    keeps masking."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=OSS, enabled=False)])
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, config), _engine(b, vault, config)
    conv = here.conversations._conv(here.conversations._name("cc:y"))
    text = f"write to {CANARY}"
    async with _ears(a, here) as hear:
        held = here.session("cc:y")
        here.attach(held, OSS)  # reported to this instance, which says so on the channel and hears itself
        await hear(1)
        assert held.reported == OSS and here.mask_text(text, SlotKind.USER_TEXT, held).text == text  # Latet off
        old = a.client.hget(conv, "w")
        there.attach(there.session("cc:y"), WORK)  # the move, through the other instance
        await hear(2)
        assert held.reported is None
        if undone == "written back":
            a.client.hset(conv, "w", old)
        else:
            a.client.hdel(conv, "w")
        seen = here.session("cc:y")  # the proxy's next request: no folder reported
        assert seen is held and CANARY not in here.mask_text(text, SlotKind.USER_TEXT, seen).text


@pytest.mark.parametrize("undone", ["deleted", "written back"])
async def test_a_holder_that_heard_of_a_later_move_writes_its_older_folder_back_nowhere(buses, undone):
    """A conversation begins in a folder with no settings of its own, reported to one instance, and moves through
    another to a folder with a term of its own, which says so on the identity's channel. Valkey's writer deletes the
    new record, or puts back the old one, before the first instance reads it: that instance, which heard of a later
    version it never read, writes its older folder back nowhere at its next update, so the other keeps masking the
    term; the other writes the new one back, and the first then takes it and masks the term too."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=WORK, terms=[TermConfig(value="Project Falcon", type="CODENAME")])])
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, config), _engine(b, vault, config)
    conv = here.conversations._conv(here.conversations._name("cc:z"))
    text = "the Project Falcon launch is on Friday"
    async with _ears(a, here, there) as hear:
        here.attach(here.session("cc:z"), OSS)  # a folder with no settings of its own, reported to this instance
        await hear(1)
        old = a.client.hget(conv, "w")
        there.attach(there.session("cc:z"), WORK)  # the move, through the other instance
        await hear(2)
        if undone == "deleted":
            a.client.hdel(conv, "w")
        else:
            a.client.hset(conv, "w", old)
        seen = here.session("cc:z")  # this instance's next request names a value new to the conversation: an update
        here.mask_text(f"write to {CANARY}", SlotKind.USER_TEXT, seen)
        assert seen.cwd == OSS and a.client.hget(conv, "w") == (None if undone == "deleted" else old)
        assert "Project Falcon" not in there.mask_text(text, SlotKind.USER_TEXT, there.session("cc:z")).text
        seen = here.session("cc:z")  # the other instance wrote the new one back
        assert seen.cwd == WORK and "Project Falcon" not in here.mask_text(text, SlotKind.USER_TEXT, seen).text


@pytest.mark.parametrize("forgotten", ["after the move was heard", "before the move was heard"])
async def test_a_holder_whose_note_of_the_conversation_was_forgotten_writes_its_older_folder_back_nowhere(
    buses, forgotten
):
    """As above, with this instance's note of the conversation gone meanwhile: its notes are a memory of a bounded size,
    which the notes of conversations it does not hold (those too heavy to hold) can fill. What it heard of the later
    version stays with the conversation it holds, and its older folder is written back nowhere."""
    a, b = buses
    config = Config(projects=[ProjectEntry(path=WORK, terms=[TermConfig(value="Project Falcon", type="CODENAME")])])
    vault = Vault(ROOT_KEY)
    here, there = _engine(a, vault, config), _engine(b, vault, config)
    conversations = here.conversations
    name = conversations._name("cc:z")
    conv = conversations._conv(name)
    text = "the Project Falcon launch is on Friday"

    def forget_the_note():
        conversations._notes.capacity = 1
        conversations.session("cc:another")  # its notes' memory holds another conversation's note now
        assert conversations._notes.get(name) is None and conversations._here.get(name) is not None

    async with _ears(a, here, there) as hear:
        here.attach(here.session("cc:z"), OSS)  # a folder with no settings of its own, reported to this instance
        await hear(1)
        if forgotten.startswith("before"):
            forget_the_note()
        there.attach(there.session("cc:z"), WORK)  # the move, through the other instance
        await hear(2)
        if forgotten.startswith("after"):
            forget_the_note()
        a.client.hdel(conv, "w")  # Valkey's writer deletes the new record
        seen = here.session("cc:z")  # this instance's next request names a value new to the conversation: an update
        here.mask_text(f"write to {CANARY}", SlotKind.USER_TEXT, seen)
        assert seen.cwd == OSS and a.client.hget(conv, "w") is None
        assert "Project Falcon" not in there.mask_text(text, SlotKind.USER_TEXT, there.session("cc:z")).text


def test_an_update_of_a_conversation_this_instance_forgot_meanwhile_writes_no_project(buses):
    """A request's conversation can leave both of this instance's memories while the request runs: its update knows
    nothing then of the versions the conversation was written at, and writes no project or folder of it, which the
    next read and update do."""
    a, _ = buses
    mine = ValkeyConversations(a, EMILY, ROOT_KEY)
    conv = mine._conv(mine._name("cc:s1"))
    session = mine.session("cc:s1")
    session.add({"e1"})
    session.project_id, session.cwd = project_id(OSS), OSS
    mine._notes.capacity = mine._here.capacity = 1
    mine.session("cc:another")  # both memories hold another conversation now
    mine.update(session, shown={"e1"})  # as the engine shows an entity
    assert not a.client.hexists(conv, "w") and a.client.hexists(conv, "e:e1")  # the entity goes all the same
    again = mine.session("cc:s1")  # read again: the next update writes the folder
    again.project_id, again.cwd = project_id(OSS), OSS
    mine.update(again)
    assert a.client.hexists(conv, "w")


async def test_a_folder_reported_after_a_move_heard_of_is_written_later_than_that_move(buses):
    """The other instance's clock runs an hour ahead, so the move it says carries a version later than this instance's
    clock. Valkey's writer deletes the record; this instance's client then reports a third folder, written at a version
    later than the move it heard of, so that the other instance takes it rather than count it as written back."""
    a, b = buses
    vault, third = Vault(ROOT_KEY), "/home/emily/side-project"
    here, there = _engine(a, vault, Config()), _engine(b, vault, Config(), clock=lambda: time.time() + 3600)
    conv = here.conversations._conv(here.conversations._name("cc:z"))
    async with _ears(a, here, there) as hear:
        here.attach(here.session("cc:z"), OSS)
        await hear(1)
        there.attach(there.session("cc:z"), WORK)
        await hear(2)
        a.client.hdel(conv, "w")
        here.attach(here.session("cc:z"), third)  # reported to this instance
        assert there.session("cc:z").cwd == third


def test_only_a_new_project_or_folder_is_announced_and_with_neither_in_it(buses, monkeypatch):
    """Announced on the identity's signed channel: the conversation's keyed name and the version, nothing else. The same
    folder reported again announces nothing, nor does writing back what Valkey lost, on the instance that wrote it or on
    one that read it, so that the reports other instances hold for an unchanged folder stay."""
    a, b = buses
    published = []

    def recorded(publish):
        def recording(channel, message):
            published.append((channel, message))
            return publish(channel, message)

        return recording

    for bus in (a, b):
        monkeypatch.setattr(bus, "publish", recorded(bus.publish))
    here, there = _engine(a, Vault(ROOT_KEY), Config()), _engine(b, Vault(ROOT_KEY), Config())
    conv = here.conversations._conv(here.conversations._name("cc:s1"))
    here.attach(here.session("cc:s1"), OSS)
    there.session("cc:s1")  # read where the other instance wrote it
    for n, engine in enumerate((here, there)):  # the instance that wrote it, then the one that read it
        a.client.hdel(conv, "w")  # Valkey lost it
        engine.show(engine.session("cc:s1"), {f"e{n}"})  # the next update writes it back
        assert a.client.hexists(conv, "w")
        engine.attach(engine.session("cc:s1"), OSS)  # the same folder again
    here.attach(here.session("cc:s1"), WORK)  # a move
    assert [channel for channel, _ in published] == [f"identity:{EMILY}"] * 2
    first, second = (message for _, message in published)
    assert set(first) == set(second) == {"kind", "conversation", "version"} and first["kind"] == "where"
    assert first["conversation"] == second["conversation"] == here.conversations._name("cc:s1")
    assert re.fullmatch(r"[0-9a-f]{32}", first["conversation"]) and first["version"] < second["version"]
    assert not any(word in repr(published) for word in (OSS, WORK, "cc:s1", project_id(OSS), project_id(WORK)))


# --- what the model wrote and the response ids, written off the caller's thread (R-7) --------------------------------


@contextlib.contextmanager
def _writes_paused(bus, ms: int = 3000):
    """Valkey holds every client's writes (``CLIENT PAUSE ... WRITE``) until the block ends, ``ms`` at most; reads still
    answer."""
    bus.client.client_pause(ms, all=False)
    try:
        yield
    finally:
        bus.client.client_unpause()


def _until(done, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not done() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert done()


async def test_a_restore_on_the_event_loop_never_waits_on_valkey_and_its_writes_land_once_it_answers(buses):
    """A small restore runs on the event loop, and writes the response's conversation and what the model wrote. With
    Valkey holding its writes, it returns in a few milliseconds; both land once Valkey answers again, and another
    instance learns them then."""
    a, b = buses
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    mediator = _instance(Vault(ROOT_KEY), ValkeyGiven(a, EMILY, ROOT_KEY), conversations=conversations)
    body = json.dumps({"model": "gpt-5", "input": f"write to {CANARY}"}).encode()
    prepared = mediator.prepare(OPENAI, "POST", "/openai/v1/responses", {}, body)
    fake = json.loads(prepared.body)["input"].removeprefix("write to ")
    transform = mediator.response_transform(prepared, "text/event-stream", None)
    assert transform is not None and prepared.session is not None and fake != CANARY
    said = {
        "type": "message",
        "id": "m1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": f"to {fake}"}],
    }
    events = (
        {"type": "response.created", "response": {"id": "resp_paused", "output": []}},
        {"type": "response.output_item.done", "output_index": 0, "item": said},
    )
    stream = b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    with _writes_paused(a):
        start = time.perf_counter()
        out = transform.feed(stream) + transform.end()
        took = time.perf_counter() - start
        assert f"to {CANARY}".encode() in out and took < 0.2
        assert theirs.named("resp_paused") is None and not conversations.flush(0.1)  # not in Valkey yet
    assert conversations.flush(5)
    assert theirs.named("resp_paused") == prepared.session.key
    assert theirs.recall(mediator.engine._key("R", f"to {CANARY}")) == f"to {fake}"


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_writes_waiting_for_valkey_are_bounded_and_the_oldest_dropped_is_counted_and_said(
    buses, monkeypatch, caplog, bound
):
    """Valkey holding its writes, the writer waits with the first; past the bound, in number or in bytes, the oldest
    waiting goes, counted and said once a minute, by number alone. What waits counts in the space's memory; once Valkey
    answers again, what was kept lands."""
    a, b = buses
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    if bound == "count":
        monkeypatch.setattr(live, "WRITES_QUEUED", 3)
    else:
        monkeypatch.setattr(live, "WRITES_QUEUED_BYTES", 36_000)  # three of the texts below, not four
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    writer = conversations._writer

    def said(n):
        return f"You said: {n} " + "x" * 10_000

    with _writes_paused(a):
        conversations.remember(bytes([0]) * 32, said(0))
        _until(lambda: writer.queued == 0)  # the writer took it, and waits on Valkey with it
        for n in range(1, 5):
            conversations.remember(bytes([n]) * 32, said(n))
        assert (writer.queued, writer.dropped) == (3, 1) and conversations.cached_bytes > 30_000
    assert conversations.flush(5) and conversations.cached_bytes == 0
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    assert [theirs.recall(bytes([n]) * 32) == said(n) for n in range(5)] == [True, False, True, True, True]
    lines = [record.getMessage() for record in caplog.records if "dropped" in record.getMessage()]
    assert len(lines) == 1 and "You said" not in lines[0]


def test_writes_of_one_record_are_one_and_what_waits_goes_in_one_round_trip(buses, monkeypatch):
    """The same record queued again replaces what waited for it; what waits goes to Valkey together."""
    a, b = buses
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    writer, batches = conversations._writer, []
    send = writer._send
    monkeypatch.setattr(writer, "_send", lambda batch: batches.append(len(batch)) or send(batch))
    with _writes_paused(a):
        conversations.name("resp_0", "cc:0")
        _until(lambda: writer.queued == 0)
        conversations.name("resp_1", "cc:a")
        conversations.name("resp_1", "cc:b")  # the same response again: one write, the latest
        for n in range(2, 12):
            conversations.remember(bytes([n]) * 32, f"You said: {n}")
        assert writer.queued == 11
    assert conversations.flush(5) and batches == [1, 11] and conversations.cached_bytes == 0
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    assert theirs.named("resp_1") == "cc:b" and theirs.recall(bytes([11]) * 32) == "You said: 11"


def test_a_round_trip_valkey_fails_is_tried_again_a_moment_later_and_said_by_its_type(buses, monkeypatch, caplog):
    """A round trip that Valkey fails goes back to the queue and is tried again ``WRITER_RETRY`` later, never at once;
    the failures are said once a minute, by the error's type alone."""
    a, b = buses
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    monkeypatch.setattr(live, "WRITER_RETRY", 0.05)
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    writer, attempts, until = conversations._writer, [], time.monotonic() + 0.25
    send = writer._send

    def flaky(batch):
        attempts.append(len(batch))
        if time.monotonic() < until:
            raise ConnectionError("Valkey at 10.0.0.5 went away")
        send(batch)

    monkeypatch.setattr(writer, "_send", flaky)
    conversations.name("resp_1", "cc:s1")
    assert conversations.flush(5) and ValkeyConversations(b, EMILY, ROOT_KEY).named("resp_1") == "cc:s1"
    assert 2 <= len(attempts) <= 10 and writer.dropped == 0
    tried = [record.getMessage() for record in caplog.records if "ConnectionError" in record.getMessage()]
    assert len(tried) == 1 and "10.0.0.5" not in tried[0]


def test_closing_sends_what_waits_and_drops_what_valkey_cannot_take_in_time(buses, monkeypatch, caplog):
    """The space closes while Valkey holds its writes a moment: what waits lands before ``close`` returns. When Valkey
    takes longer than ``WRITER_CLOSE``, what still waits then is dropped (what is under way lands still); and once
    closing, a round trip that Valkey fails is dropped, never tried again. Each drop counted and said."""
    a, b = buses
    caplog.set_level(logging.WARNING, logger="latet.server.live")
    conversations, theirs = ValkeyConversations(a, EMILY, ROOT_KEY), ValkeyConversations(b, EMILY, ROOT_KEY)
    writer = conversations._writer
    a.client.client_pause(300, all=False)  # Valkey answers again by itself, 0.3 s from now
    try:
        conversations.name("resp_1", "cc:s1")
        _until(lambda: writer.queued == 0)  # taken: the writer waits on Valkey with it
        conversations.name("resp_2", "cc:s2")  # waits in the queue
        conversations.close()
    finally:
        a.client.client_unpause()
    assert (theirs.named("resp_1"), theirs.named("resp_2"), writer.dropped) == ("cc:s1", "cc:s2", 0)
    monkeypatch.setattr(live, "WRITER_CLOSE", 0.3)
    send = writer._send

    def slow(batch):
        time.sleep(1.0)
        send(batch)

    monkeypatch.setattr(writer, "_send", slow)
    conversations.name("resp_3", "cc:s3")
    _until(lambda: writer.queued == 0)  # under way, slowly
    conversations.name("resp_4", "cc:s4")
    start = time.monotonic()
    conversations.close()
    assert time.monotonic() - start < 0.9 and writer.dropped == 1  # resp_4, still waiting when the wait ran out
    assert conversations.flush(5) and (theirs.named("resp_3"), theirs.named("resp_4")) == ("cc:s3", None)

    def gone(batch):
        raise ConnectionError("Valkey went away")

    monkeypatch.setattr(writer, "_send", gone)
    conversations.name("resp_5", "cc:s5")
    _until(lambda: (writer.queued, writer.dropped) == (0, 2))
    assert conversations.flush(5) and theirs.named("resp_5") is None
    assert len([record for record in caplog.records if "dropped" in record.getMessage()]) == 1  # once a minute


def test_the_writer_ends_with_nothing_to_write_and_starts_again_at_the_next_write(buses, monkeypatch):
    """A space that writes nothing holds no thread for it."""
    a, b = buses
    monkeypatch.setattr(live, "WRITER_IDLE", 0.05)
    conversations = ValkeyConversations(a, EMILY, ROOT_KEY)
    writer = conversations._writer
    assert writer._thread is None
    conversations.name("resp_1", "cc:s1")
    assert conversations.flush(5)
    _until(lambda: writer._thread is None)
    conversations.name("resp_2", "cc:s2")
    assert conversations.flush(5)
    theirs = ValkeyConversations(b, EMILY, ROOT_KEY)
    assert (theirs.named("resp_1"), theirs.named("resp_2")) == ("cc:s1", "cc:s2")
