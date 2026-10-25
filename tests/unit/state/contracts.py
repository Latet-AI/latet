"""What every implementation of latet.state's protocols must do: on a computer (test_local.py) and on a Latet server
(tests/server, E0b). Each contract asks for two views of the same state, as two processes see it (``pair``, or
``stores`` for the stores): on a computer one process holds it, and the two views are one object (the stores: two
stores over one database file)."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from latet.config import Config, Profile, TermConfig
from latet.engine.pipeline import personal_values
from latet.engine.types import EntityClass, Policy, Replacement
from latet.engine.vault import Entity, EntryGone, Vault
from latet.gateway.approvals import Batch, Change, Decision, HumanMask
from latet.gateway.aws import AwsError
from latet.gateway.sigv4 import AwsCredentials
from latet.state import Left
from latet.state.local import CODE_TTL, SESSION_TTL
from latet.storage.exchanges import BlockRecord, ExchangeRecord, ImageRecord

from ...conftest import ROOT_KEY


class Clock:
    """A clock the test moves."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _soon(read: Callable[[], Any], within: float = 5.0) -> Any:
    """What one process reads of what another wrote a moment before (a server writes what the model wrote and the
    response ids off the caller's thread): read again until it is there, for a few seconds at most."""
    deadline = time.monotonic() + within
    while (found := read()) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    return found


def _summary(ts: float, session: str = "s", flags: tuple[str, ...] = (), upstream: str = "openai") -> dict[str, Any]:
    """An exchange's summary as the recorder writes it, with what the history reads of it."""
    return {"ts": ts, "session": session, "agent": "codex", "flags": list(flags), "new_masked": 1, "upstream": upstream}


class JobLockContract:
    def test_a_job_runs_in_one_process_at_a_time(self, pair):
        a, b = pair
        with a.hold("history-prune", 60) as mine:
            assert mine
            with b.hold("history-prune", 60) as theirs:
                assert not theirs
            with b.hold("terms-sync", 60) as other:
                assert other  # another job is free
        with b.hold("history-prune", 60) as again:
            assert again

    def test_a_job_that_fails_frees_its_lock(self, pair):
        a, b = pair

        def fails():
            with a.hold("history-prune", 60) as mine:
                assert mine
                raise RuntimeError("the job failed")

        with pytest.raises(RuntimeError, match="the job failed"):
            fails()
        with b.hold("history-prune", 60) as theirs:
            assert theirs


def _showing(value: str, fake: str) -> Entity:
    """An entry kept before the vault refused a fake that shows its value: a user name and its old fake."""
    entity_id = Vault(ROOT_KEY).entity_id("USERNAME", value)
    return Entity(entity_id, "USERNAME", EntityClass.IDENTITY, value, fake, 0, time.time(), "identity:username")


def _sound() -> Entity:
    """An entry whose fake shows nothing of its value."""
    entity_id = Vault(ROOT_KEY).entity_id("PASSWORD", "hunter2-Lavender")
    return Entity(entity_id, "PASSWORD", EntityClass.SECRET, "hunter2-Lavender", "pw_a1b2c3", 0, time.time(), None)


class VaultStoreContract:
    """``stores``: two stores over the same vault, with the same root key; ``stranger``: one over it with another."""

    def test_a_value_kept_by_one_process_is_loaded_by_another(self, stores):
        a, b = stores
        vault = Vault(ROOT_KEY, a.load_entities(), on_insert=a.save_entity, on_rule=a.save_rule)
        entity = vault.intern("EMAIL", EntityClass.PII, "emily.carter@acme.com", rule="pii:email")
        assert b.load_entity(entity.id) == entity and entity in b.load_entities()
        assert b.load_entity("0" * 20) is None

    def test_the_same_new_value_masked_by_two_processes_at_once_gets_one_id_and_one_fake(self, stores):
        a, b = stores
        mine = Vault(ROOT_KEY, a.load_entities(), on_insert=a.save_entity)
        theirs = Vault(ROOT_KEY, b.load_entities(), on_insert=b.save_entity)  # made before: it knows nothing of it
        first = mine.intern("PERSON", EntityClass.NER, "Emily Carter")
        # The other process's text holds the first fake already: its own first try is refused there, and the store
        # gives it the value kept first.
        second = theirs.intern("PERSON", EntityClass.NER, "Emily Carter", context=f"ask {first.surrogate} today")
        assert second == first and theirs.by_surrogate(first.surrogate) == first
        # What the vault holds, not only what it answered: asked again, it gives the value kept first.
        assert theirs.intern("PERSON", EntityClass.NER, "Emily Carter") == first

    def test_a_fake_another_process_gave_to_another_value_is_not_given_twice(self, stores):
        a, b = stores
        mine = Vault(ROOT_KEY, a.load_entities(), on_insert=a.save_entity)
        theirs = Vault(ROOT_KEY, b.load_entities(), on_insert=b.save_entity)
        taken = mine.intern("PROJECT", EntityClass.CUSTOM, "Orion", generate=lambda original, rng: "Vega-7")
        fakes = iter(["Vega-7", "Altair-3"])
        other = theirs.intern("PROJECT", EntityClass.CUSTOM, "Falcon", generate=lambda original, rng: next(fakes))
        assert (taken.surrogate, other.surrogate, other.counter) == ("Vega-7", "Altair-3", 1)
        # What the vault holds, not only what it answered: the fake the store refused was never indexed.
        assert theirs.by_surrogate("Vega-7") is None and theirs.intern("PROJECT", EntityClass.CUSTOM, "Falcon") == other

    def test_a_value_forgotten_by_one_process_is_gone_for_the_other(self, stores):
        a, b = stores
        entity = Vault(ROOT_KEY, on_insert=a.save_entity).intern("EMAIL", EntityClass.PII, "emily@gmail.com")
        a.forget_entity(entity.id)
        assert b.load_entity(entity.id) is None and b.load_entities() == []

    def test_an_entry_keeps_whether_its_value_was_found_as_written_or_where_escapes_read(self, stores):
        # The reading of a fake written in escapes is made from it: another process, and this one after a restart, give
        # the same fake the same reading.
        from latet.engine.vault import fake_reading

        a, b = stores
        vault = Vault(ROOT_KEY, a.load_entities(), on_insert=a.save_entity)
        plain = vault.intern("PHONE", EntityClass.PII, "333 1234567", rule="pii:phone")
        written = vault.intern("PHONE", EntityClass.PII, "333+1234567", rule="known", reads="333 1234567")
        assert (plain.decoded, written.decoded) == (False, True)
        assert b.load_entity(plain.id) == plain and b.load_entity(written.id) == written
        again = Vault(ROOT_KEY, b.load_entities())
        reading = fake_reading(written)
        assert reading is not None and again.is_surrogate(reading) and fake_reading(plain) is None

    def test_a_rule_learned_later_is_kept(self, stores):
        a, b = stores
        vault = Vault(ROOT_KEY, on_insert=a.save_entity, on_rule=a.save_rule)
        entity = vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com")  # no rule: as recorded before rules were kept
        vault.intern("EMAIL", EntityClass.PII, "a.b@gmail.com", rule="pii:email")
        found = b.load_entity(entity.id)
        assert found is not None and found.rule == "pii:email"

    def test_the_reviewers_answers_and_your_image_decisions_are_kept_for_both(self, stores):
        a, b = stores
        a.save_review(b"k" * 32, {"findings": [["Ada Byron", "person"]], "block": False})
        assert b.load_review(b"k" * 32) == {"findings": [["Ada Byron", "person"]], "block": False}
        b.forget_reviews()
        assert a.load_review(b"k" * 32) is None
        a.save_image_decision("f" * 64, {"rects": [[0, 0, 10, 10]], "withhold": False})
        assert b.load_image_decision("f" * 64) == {"rects": [[0, 0, 10, 10]], "withhold": False}
        assert b.load_image_decision("e" * 64) is None

    def test_values_sealed_with_another_key_are_skipped_and_counted(self, stores, stranger):
        a, _ = stores
        Vault(ROOT_KEY, on_insert=a.save_entity).intern("EMAIL", EntityClass.PII, "emily@gmail.com")
        assert stranger.load_entities() == [] and stranger.unreadable == 1

    def test_an_entry_whose_fake_shows_its_value_gets_a_new_fake_at_load_kept_so(self, stores):
        """A vault kept before the vault refused such fakes holds `emily → emily59`: loaded, the entry gets the fake
        its value gets now, kept in the store in its place, and seen so by another process. A sound entry is
        untouched."""
        a, b = stores
        old, sound = _showing("emily", "emily59"), _sound()
        assert a.save_entity(old) is None and a.save_entity(sound) is None
        vault = Vault(ROOT_KEY, a.load_entities(), on_insert=a.save_entity, on_replace=a.replace_fake)
        new = vault.by_original("emily")
        assert new is not None and new.id == old.id and "emily" not in new.surrogate.casefold()
        assert vault.by_surrogate("emily59") is None and vault.by_surrogate(new.surrogate) == new
        assert b.load_entity(old.id) == new and b.load_entity(sound.id) == sound
        again = Vault(ROOT_KEY, b.load_entities(), on_replace=b.replace_fake)  # loaded again: nothing to draw
        assert again.by_original("emily") == new and again.by_original(sound.original) == sound

    def test_a_new_fake_another_process_kept_first_is_the_one_used(self, stores, monkeypatch):
        """Two processes load the same old entry: the second, which would draw another fake (an older generator, say),
        finds the entry changed by the first and uses its fake, never puts its own in its place."""
        a, b = stores
        a.save_entity(_showing("emily", "emily59"))
        found = b.load_entities()  # read before the first process loads it again
        first = Vault(ROOT_KEY, a.load_entities(), on_replace=a.replace_fake).by_original("emily")
        monkeypatch.setattr(
            Vault, "_generate", lambda self, etype, eclass, original, counter, *rest: f"elsewhere{counter}"
        )
        second = Vault(ROOT_KEY, found, on_replace=b.replace_fake).by_original("emily")
        assert first is not None and second == first and a.load_entity(first.id) == first

    def test_an_entry_forgotten_before_its_new_fake_is_kept_is_gone_for_the_vault_too(self, stores):
        """Another process forgets the entry between this vault's reading and its new fake: the store says the entry is
        gone, never that the fake was kept, and the vault forgets it as well; the value is known nowhere."""
        a, b = stores
        old = _showing("emily", "emily59")
        a.save_entity(old)
        found = a.load_entities()
        b.forget_entity(old.id)
        with pytest.raises(EntryGone):
            a.replace_fake(old, replace(old, surrogate="ttenney", counter=1))
        vault = Vault(ROOT_KEY, found, on_replace=a.replace_fake)
        assert (
            vault.get(old.id) is None and vault.by_original("emily") is None and vault.by_surrogate("emily59") is None
        )
        assert b.load_entity(old.id) is None and b.load_entities() == []

    def test_a_new_fake_the_store_knows_already_is_drawn_again_with_the_next_counter(self, stores):
        a, b = stores
        a.save_entity(_showing("emily", "emily59"))
        found = a.load_entities()
        taken = Vault(ROOT_KEY)._generate("USERNAME", EntityClass.IDENTITY, "emily", 0, None)
        b.save_entity(replace(_sound(), surrogate=taken))  # another process gave it to another value meanwhile
        new = Vault(ROOT_KEY, found, on_replace=a.replace_fake).by_original("emily")
        assert new is not None and new.surrogate != taken and new.counter > 0 and b.load_entity(new.id) == new


class HistoryStoreContract:
    """``stores``: two history stores over the same history, with the same root key."""

    def test_an_exchange_recorded_by_one_process_is_listed_and_shown_by_another(self, stores):
        a, b = stores
        text = "history CANARY-TEXT-1234567890"
        block = BlockRecord(a.block_id(text), "$.input[0]", "user_text", False, True, text, [])
        response = {"headers": [], "text": "You said: history", "text_spans": [], "tool_calls": []}
        record = ExchangeRecord(_summary(time.time()), [block], request_headers=[("user-agent", "codex")])
        record.response = response
        exchange = a.record(record)
        [listed], cursor = b.list()
        assert listed["id"] == exchange and cursor is None and b.block_id(text) == block.id
        detail = b.detail(exchange)
        assert detail is not None and [s["masked"] for s in detail["request"]["slots"]] == [text]
        assert detail["response"]["text"] == "You said: history"
        assert detail["request"]["headers"] == [["user-agent", "codex"]] and b.detail("ex_none") is None
        assert b.known_blocks([block.id, "a" * 32]) == {block.id}

    def test_the_tool_of_a_tool_output_is_kept_with_the_exchange(self, stores):
        """What strict data mode covers depends on the tool (files and web pages are left alone): the dashboard's
        columns answer reads it from here. A block recorded with no tool, as every block before, has none."""
        a, b = stores
        read = BlockRecord(a.block_id('{"id": 1}'), "$.messages[2].content", "tool_result", False, True, '{"id": 1}')
        read.tool = "Read"
        prompt = BlockRecord(a.block_id("show me"), "$.messages[0].content", "user_text", False, True, "show me")
        exchange = a.record(ExchangeRecord(_summary(time.time()), [prompt, read]))
        detail = b.detail(exchange)
        assert detail is not None
        assert [(s["kind"], s["tool"]) for s in detail["request"]["slots"]] == [
            ("user_text", None),
            ("tool_result", "Read"),
        ]

    def test_pages_filters_sessions_and_the_order_of_a_conversation(self, stores):
        a, b = stores
        now = time.time()
        ids = [a.record(ExchangeRecord(_summary(now + i, flags=("blocked",) if i == 3 else ()))) for i in range(5)]
        page, cursor = b.list(limit=2)
        assert [p["id"] for p in page] == [ids[4], ids[3]]
        page, cursor = b.list(limit=2, before=cursor)
        assert [p["id"] for p in page] == [ids[2], ids[1]]
        assert [p["id"] for p in b.list(flag="blocked")[0]] == [ids[3]] and b.list(agent="claude-code")[0] == []
        [session] = b.sessions()
        assert (session["key"], session["exchanges"], session["blocked"], session["masked"]) == ("s", 5, 1, 5)
        later = a.record(ExchangeRecord(_summary(now - 5)))  # a clock set back meanwhile: recorded last all the same
        assert b.in_order("s") == [*ids, later] and b.in_order("nobody") == []

    def test_the_reviewers_records_are_shown_to_both(self, stores):
        a, b = stores
        summary = {
            "ts": time.time(), "session": "s", "project": None, "block": "b" * 32, "kind": "user_text",
            "inline": True, "findings": [{"category": "person", "masked": True}],
        }  # fmt: skip
        review = a.record_review(summary)
        [found] = b.reviews(session="s")
        assert found["id"] == review and found["findings"] == [{"category": "person", "masked": True}]
        assert b.reviews(session="other") == []

    def test_an_image_as_it_was_sent_is_kept_once(self, stores):
        a, b = stores
        data = base64.b64encode(b"PNG, not really").decode()
        image = ImageRecord("$.messages[0]", "masked", a.image_id(data), "image/png", data)
        a.record(ExchangeRecord(_summary(time.time()), images=[image]))
        assert b.image(a.image_id(data)) == ("image/png", b"PNG, not really") and b.image("im_none") is None

    def test_values_are_counted_by_type_in_the_stored_blocks(self, stores):
        a, b = stores
        replacements = [{"masked_start": 0, "masked_end": 3, "type": "EMAIL", "class": "PII", "rule": "pii:email"}]
        block = BlockRecord(a.block_id("abc def"), "$", "user_text", False, True, "abc def", replacements)
        a.record(ExchangeRecord(_summary(time.time()), [block]))
        assert b.block_types([block.id]) == {block.id: [("EMAIL", "PII", "personal", 1)]}

    def test_when_each_route_was_used_last_and_what_was_recorded_since(self, stores):
        a, b = stores
        now = time.time()
        a.record(ExchangeRecord(_summary(now - 100, upstream="anthropic")))
        a.record(ExchangeRecord(_summary(now, upstream="anthropic")))
        a.record(ExchangeRecord(_summary(now - 50, upstream="openai")))
        assert b.last_used() == {"anthropic": now, "openai": now - 50}
        assert [s["upstream"] for s in b.summaries_since(now - 60)] == ["openai", "anthropic"]

    def test_the_stats_read_the_latest_summaries_a_part_at_a_time_and_no_more(self, stores):
        """``summaries_parts``, what the dashboard's stats read: from ``ts`` on, the latest ``most`` at most, the newest
        first, a part at a time (never the whole history at once)."""
        a, b = stores
        now = time.time()
        for n, upstream in enumerate(("before", "anthropic", "openai", "bedrock")):
            a.record(ExchangeRecord(_summary(now - 100 + n * 10, upstream=upstream)))
        parts = list(b.summaries_parts(now - 95, 2))
        assert all(isinstance(part, list) and part for part in parts)
        assert [s["upstream"] for part in parts for s in part] == ["bedrock", "openai"]
        every = [s["upstream"] for part in b.summaries_parts(now - 95, 10) for s in part]
        assert every == ["bedrock", "openai", "anthropic"]

    def test_what_is_past_the_retention_goes_and_clearing_leaves_nothing(self, stores):
        a, b = stores
        a.retention_days = 14
        old = BlockRecord(a.block_id("old"), "$", "user_text", False, True, "old", [])
        a.record(ExchangeRecord(_summary(time.time() - 20 * 86400), [old]))
        recent = a.record(ExchangeRecord(_summary(time.time())))
        assert a.prune() == 1 and [s["id"] for s in b.list()[0]] == [recent]
        assert b.known_blocks([old.id]) == {old.id}  # carried today: kept until its own time
        a.prune(now=time.time() + 15 * 86400)
        assert b.known_blocks([old.id]) == set()
        a.record(ExchangeRecord(_summary(time.time())))
        b.clear()
        assert a.list() == ([], None)

    def test_a_retention_assigned_later_is_the_one_the_next_prune_uses(self, stores):
        """The daemon assigns ``retention_days`` when you change the setting while it runs."""
        a, b = stores
        now = time.time()
        a.retention_days = 14
        ten = a.record(ExchangeRecord(_summary(now - 10 * 86400)))
        assert a.prune(now=now) == 0 and [s["id"] for s in b.list()[0]] == [ten]  # 10 days old: kept at 14
        a.retention_days = 7
        assert a.prune(now=now) == 1 and b.list() == ([], None)  # the same prune: 10 days is past 7


class ConversationsContract:
    def test_a_conversation_is_made_at_its_first_use(self, pair):
        a, _ = pair
        session = a.session("conv-new")
        assert (session.key, session.entity_ids, session.project_id, session.approved) == (
            "conv-new",
            set(),
            None,
            set(),
        )

    def test_what_a_conversation_was_shown_reaches_every_process(self, pair):
        a, b = pair
        session = a.session("conv-1")
        session.add({"e1", "e2"})
        session.project_id, session.cwd = "p1", "/work/billing-api"
        session.approved.add("ab12")
        a.update(session)
        seen = b.session("conv-1")
        assert seen.entity_ids == {"e1", "e2"} and (seen.project_id, seen.cwd) == ("p1", "/work/billing-api")
        assert seen.approved == {"ab12"} and "conv-1" in {s.key for s in b.sessions()}

    def test_what_one_conversation_holds_stays_out_of_another(self, pair):
        a, b = pair
        session = a.session("conv-1")
        session.add({"e1"})
        session.project_id, session.cwd = "p1", "/work/billing-api"
        session.approved.add("ab12")
        a.update(session)
        other = b.session("conv-10")  # its key begins with the first one's
        assert (other.entity_ids, other.approved, other.project_id) == (set(), set(), None)

    def test_two_processes_adding_to_one_conversation_keep_both(self, pair):
        a, b = pair
        mine, theirs = a.session("conv-2"), b.session("conv-2")
        mine.add({"e1"})
        mine.approved.add("ab12")
        theirs.add({"e2"})
        theirs.approved.add("cd34")
        a.update(mine)
        b.update(theirs)
        kept = a.session("conv-2")
        assert (kept.entity_ids, kept.approved) == ({"e1", "e2"}, {"ab12", "cd34"})

    def test_what_the_model_wrote_is_recalled_by_its_restored_form(self, pair):
        a, b = pair
        a.remember(b"R" * 32, "You said: mail Rachel.Huang@fourthcoffee.example")
        assert _soon(lambda: b.recall(b"R" * 32)) == "You said: mail Rachel.Huang@fourthcoffee.example"
        assert b.recall(b"S" * 32) is None

    def test_a_response_names_its_conversation(self, pair):
        a, b = pair
        a.name("resp_1", "conv-1")
        assert _soon(lambda: b.named("resp_1")) == "conv-1" and b.named("resp_2") is None


class SentBlocksContract:
    def test_a_block_is_new_once_in_each_conversation(self, pair):
        a, b = pair
        assert a.first("conv-1", "b" * 32) and not b.first("conv-1", "b" * 32)
        assert b.first("conv-2", "b" * 32) and not a.first("conv-2", "b" * 32)


def _as_it_leaves(left: Left | None) -> tuple[str, bool, list[Replacement]] | None:
    """What a text as it left must keep to leave again the same: its text, whether its offsets fit, and each replacement
    but its value. An implementation may keep no value (a server keeps the entity's id, or for a value with none a keyed
    hash of it, which tells values apart as the dump guard counts them)."""
    if left is None:
        return None
    return left.text, left.exact, [replace(r, original="") for r in left.replacements]


class GivenContract:
    """``pair``: two views of what one conversation's texts were when they left, as two processes see them."""

    def test_a_text_that_left_in_one_process_leaves_the_same_in_the_other(self, pair):
        a, b = pair
        key = b"k" * 32
        assert b.masked(key) is None
        replaced = Replacement(
            9,
            30,
            "emily.carter@acme.com",
            "EMAIL_1",
            "EMAIL",
            EntityClass.PII,
            "pii:email",
            Policy.MASK,
            "e" + "1" * 19,
        )
        left = Left("write to EMAIL_1", (replaced,), True)
        a.keep(key, left)
        assert _as_it_leaves(b.masked(key)) == _as_it_leaves(left) and b.masked(key) == a.masked(key)
        assert b.masked(b"other" * 6 + b"xx") is None

    def test_the_other_process_counts_as_many_values_in_a_text_as_it_left_as_this_one(self, pair):
        """What the dump guard counts in a text sent as it left: two different values with no entity (redacted) stay
        two, and one value twice stays one, whatever an implementation keeps in a value's place."""
        a, b = pair
        key = b"v" * 32
        original = "call +1 555 0100 or +1 555 0199, and +1 555 0100 first"
        found = (original.index("+1 555 0100"), original.index("+1 555 0199"), original.rindex("+1 555 0100"))
        replacements = tuple(
            Replacement(
                at, at + 11, original[at : at + 11], "[REDACTED:PHONE]", "PHONE", EntityClass.PII, "pii:phone",
                Policy.REDACT, None,
            )
            for at in found
        )  # fmt: skip
        left = Left("call [REDACTED:PHONE] or [REDACTED:PHONE], and [REDACTED:PHONE] first", replacements, True)
        a.keep(key, left)
        assert personal_values(b.masked(key).replacements) == personal_values(left.replacements) == 2
        assert _as_it_leaves(b.masked(key)) == _as_it_leaves(left)

    def test_a_block_the_final_check_let_leave_in_one_process_is_known_to_the_other(self, pair):
        a, b = pair
        one, two = b"1" * 32, b"2" * 32
        assert not b.checked("cc:s1", one)
        a.keep_checked("cc:s1", [one, two])
        assert b.checked("cc:s1", one) and b.checked("cc:s1", two)
        assert not b.checked("cc:s2", one)  # another conversation: it did not leave there
        a.keep_checked("cc:s2", [])
        assert not a.checked("cc:s2", one)


class FeedContract:
    async def test_what_one_process_publishes_reaches_the_viewers_of_another(self, pair):
        a, b = pair
        queue = b.subscribe()
        a.publish({"id": "ex_1"})
        assert await asyncio.wait_for(queue.get(), 5) == {"id": "ex_1"}
        a.publish({"id": "rv_1"}, "review")
        assert await asyncio.wait_for(queue.get(), 5) == ("review", {"id": "rv_1"})
        for n in range(20):
            a.publish({"n": n})
        assert [(await asyncio.wait_for(queue.get(), 5))["n"] for _ in range(20)] == list(range(20))
        b.unsubscribe(queue)
        a.publish({"id": "ex_2"})
        await asyncio.sleep(0.2)
        assert queue.empty()

    async def test_a_viewer_that_stops_reading_neither_stops_the_feed_nor_the_other_viewers(self, pair):
        """What a stalled viewer holds is its own business (a computer's keeps the first 500 events and misses the
        rest): the feed goes on, for the others and for the process that publishes."""
        a, b = pair
        stalled, reading = b.subscribe(), b.subscribe()  # the first one never reads
        heard = []
        for n in range(1_000):  # far more than a viewer's queue holds
            a.publish({"n": n})  # returns, and does not fail, whatever the stalled viewer's queue holds
            heard.append((await asyncio.wait_for(reading.get(), 5))["n"])
        assert heard == list(range(1_000))
        b.unsubscribe(stalled)
        b.unsubscribe(reading)


TEXT = "call Ada Byron on Monday about the Orion contract"


def _batch(**kwargs: Any) -> Batch:
    start = TEXT.index("Monday")
    item = {
        "kind": "user_text", "path": None, "tool": None, "original": TEXT,
        "masked": TEXT.replace("Monday", "Friday"),
        "replacements": [{"start": start, "end": start + 6, "masked_start": start, "masked_end": start + 6}],
        "suggestions": [], "failed": None,
    }  # fmt: skip
    return Batch("cc:s", None, "claude-code", [item], **kwargs)


class _Image:
    """An image of a batch, as the Live Reviewer keeps it: as it came, and as it would leave."""

    def __init__(self, original: str, sent: str, media: str = "image/png") -> None:
        self.original = (original, media)
        self.ref = SimpleNamespace(data=lambda: sent, media_type=media)


class ApprovalsContract:
    async def test_a_batch_stored_waits_and_is_told_once_it_can_be_read(self, pair):
        """``store``, which the Live Reviewer's requests await (``Approvals.open``): the batch waits, then ``announce``
        is called once, with the batch readable by every process, to tell the dashboards."""
        a, b = pair
        batch = _batch(polled=True)
        readable: list[bool] = []
        await a.store(batch, lambda: readable.append(b.get(batch.id) is not None))
        assert readable == [True] and [x.id for x in b.waiting()] == [batch.id] and not batch.future.done()
        assert b.settle(batch.id, Decision(True))
        decision = await a.decision(batch, 5)
        assert decision is not None and decision.approved
        quiet = _batch()
        await a.store(quiet)  # nobody to tell
        assert [x.id for x in b.waiting()] == [quiet.id]

    async def test_a_batch_put_by_one_process_is_listed_and_decided_by_another(self, pair):
        a, b = pair
        batch = _batch()
        await a.store(batch)
        assert [x.id for x in b.waiting()] == [batch.id]
        seen = b.get(batch.id)
        assert seen is not None and seen.summary() == batch.summary()
        masks = [HumanMask(0, "Ada Byron", "person")]
        assert b.settle(batch.id, Decision(True, masks)) and not a.settle(batch.id, Decision(False))
        decision = await a.decision(batch, 5)
        assert decision is not None and decision.approved and decision.masks == masks
        assert a.waiting() == [] and b.get(batch.id) is None

    async def test_approving_every_batch_settles_each_once_wakes_its_wait_and_tells_of_each(self, pair):
        """``settle_all``, a dashboard's "approve all": each batch still waiting settled once, the request that waits
        for it woken in any process, ``told`` called for each one settled (the dashboards' "settled"), and how many."""
        a, b = pair
        first, second = _batch(polled=True), _batch()
        await a.store(first)
        await a.store(second)
        waiting = asyncio.ensure_future(a.decision(first, 10))
        told: list[str] = []
        decisions = [(first.id, Decision(True)), (second.id, Decision(True))]
        assert await b.settle_all(decisions, lambda batch_id, decision: told.append(batch_id)) == 2
        decision = await asyncio.wait_for(waiting, 10)
        assert decision is not None and decision.approved and sorted(told) == sorted([first.id, second.id])
        assert a.waiting() == [] and b.waiting() == []
        assert await a.settle_all(decisions, lambda batch_id, decision: told.append("again")) == 0
        assert "again" not in told

    async def test_a_waiting_batch_is_shown_masked_again_and_a_decided_one_never_changes(self, pair):
        """``replace``, when the rules or the vault change while a batch waits (the process that opened it masks its
        texts again): every process reads the new previews, as old as the batch was; once it is decided, nothing is
        replaced."""
        a, b = pair
        batch = _batch(polled=True)
        await a.store(batch)
        again = [dict(batch.items[0], masked=TEXT.replace("Ada Byron", "Eva Moore"))]
        updated = await a.replace(batch.id, again)
        assert updated is not None and updated.items == again and updated.id == batch.id
        seen = b.get(batch.id)
        assert seen is not None and seen.items == again and seen.created == batch.created
        assert [x.items for x in b.waiting()] == [again]
        assert b.settle(batch.id, Decision(True))
        assert await a.replace(batch.id, [dict(again[0], masked=TEXT)]) is None
        assert b.get(batch.id) is None

    async def test_the_batches_waiting_are_listed_oldest_first(self, pair):
        a, b = pair
        newer, older = _batch(), _batch()
        older.created -= 60  # it has waited a minute longer, and is put second
        await a.store(newer)
        await a.store(older)
        assert [x.id for x in b.waiting()] == [older.id, newer.id]

    async def test_a_decision_wakes_a_request_already_waiting_in_another_process(self, pair):
        a, b = pair
        batch = _batch(polled=True)
        await a.store(batch)
        waiting = asyncio.ensure_future(a.decision(batch, 10))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        b.settle(batch.id, Decision(False, reason="you did not approve it", state="rejected"))
        decision = await asyncio.wait_for(waiting, 10)
        assert decision is not None and (decision.state, decision.reason) == ("rejected", "you did not approve it")
        # The request that was woken gives its answer at once: the batch left the waiting list before it was woken.
        answer = {"items": [{"text": "[withheld by Latet: you did not approve it]", "count": 0}], "degraded": []}
        a.give(batch.id, answer)
        assert await b.answer(batch.id, 5) == answer

    async def test_a_wait_gives_up_when_its_time_passes(self, pair):
        a, _ = pair
        batch = _batch()
        await a.store(batch)
        assert await a.decision(batch, 0.05) is None

    async def test_a_decision_taken_after_a_wait_gave_up_is_there_when_the_request_asks_again(self, pair):
        a, b = pair
        batch = _batch()
        await a.store(batch)
        assert await a.decision(batch, 0.05) is None  # the plugin's poll ended: it asks again
        assert b.settle(batch.id, Decision(True))
        decision = await a.decision(batch, 5)
        assert decision is not None and decision.approved

    async def test_the_plugins_answer_reaches_any_process(self, pair):
        a, b = pair
        batch = _batch(polled=True)
        await a.store(batch)
        assert await b.answer(batch.id, 0.05) is None  # not decided yet: asked again
        a.settle(batch.id, Decision(True))
        a.give(batch.id, {"items": [{"text": "masked", "count": 1}], "degraded": []})
        assert await b.answer(batch.id, 5) == {"items": [{"text": "masked", "count": 1}], "degraded": []}
        with pytest.raises(KeyError):  # handed over once: the call is forgotten with it, and a later poll finds nothing
            await b.answer(batch.id, 0.05)
        with pytest.raises(KeyError):
            await b.answer("nothing-with-this-id", 0.05)

    async def test_an_answer_is_not_taken_while_its_batch_waits_for_you(self, pair):
        a, b = pair
        batch = _batch(polled=True)
        await a.store(batch)
        answer = {"items": [{"text": "call Ada Byron on Monday", "count": 0}], "degraded": []}
        with pytest.raises(ValueError, match="wait"):  # the answer is what leaves, and only your decision lets it
            b.give(batch.id, answer)
        assert await a.answer(batch.id, 0.05) is None  # nothing was kept: the plugin is told to ask again
        a.settle(batch.id, Decision(True))
        b.give(batch.id, answer)  # decided: the same answer is kept now
        assert await a.answer(batch.id, 5) == answer

    async def test_an_answer_given_where_no_call_waits_for_one_is_dropped_without_an_error(self, pair):
        """A call given up and forgotten meanwhile still has its answer worked out and given: nobody will ask for it,
        so it is no error, and nothing of it is kept for a poll that comes later."""
        a, b = pair
        answer = {"items": [{"text": "masked", "count": 1}], "degraded": []}
        a.give("nothing-with-this-id", answer)
        with pytest.raises(KeyError):
            await b.answer("nothing-with-this-id", 0.05)
        unasked = _batch()  # a request's own batch, which the plugin does not ask about
        await a.store(unasked)
        a.settle(unasked.id, Decision(True))
        b.give(unasked.id, answer)
        with pytest.raises(KeyError):
            await a.answer(unasked.id, 0.05)
        handed = _batch(polled=True)
        await a.store(handed)
        a.settle(handed.id, Decision(True))
        a.give(handed.id, answer)
        assert await b.answer(handed.id, 5) == answer
        a.give(handed.id, answer)  # once more, after it was handed over: not kept for another poll
        with pytest.raises(KeyError):
            await b.answer(handed.id, 0.05)

    async def test_a_poll_counts_as_a_sign_of_life(self, pair):
        a, b = pair
        batch = _batch(polled=True)
        batch.seen -= 600  # nobody asked about it for ten minutes: a poll is what makes it seen now
        await a.store(batch)
        before = time.monotonic()
        a.polled(batch, 1)
        assert b.get(batch.id).waiting == 1  # a poll is open: the reaper does not give the batch up
        a.polled(batch, -1)
        seen = b.get(batch.id)
        assert seen is not None and seen.seen >= before - 1 and seen.waiting == 0

    async def test_the_reaper_reads_of_each_batch_held_here_its_times_and_its_polls_and_never_its_texts(self, pair):
        """``timings``: of each batch this process holds, when it was made, its time limit, whether the plugin asks for
        it, how many ask now in every process and when one last did; once the batch is decided, nothing."""
        a, b = pair
        batch = _batch(polled=True, timeout_s=60)
        batch.created -= 30
        await a.store(batch)
        [timing] = a.timings()
        assert (timing.id, timing.created, timing.timeout_s, timing.polled) == (batch.id, batch.created, 60, True)
        assert not hasattr(timing, "items")  # never its texts
        before = time.monotonic()
        b.polled(batch, 1)
        [timing] = a.timings()
        assert timing.waiting == 1 and timing.seen >= before - 1  # a poll is open: the reaper does not give it up
        b.polled(batch, -1)
        assert a.timings()[0].waiting == 0
        assert b.settle(batch.id, Decision(False, reason="you did not approve it", state="rejected"))
        assert a.timings() == []

    async def test_an_image_waiting_is_shown_as_it_came_and_as_it_would_leave(self, pair):
        a, b = pair
        batch = _batch()
        batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
        await a.store(batch)
        assert b.image(batch.id, 0, sent=False) == ("b3JpZ2luYWw=", "image/png")
        assert b.image(batch.id, 0, sent=True) == ("c2VudA==", "image/png")
        assert b.image(batch.id, 1, sent=False) is None and b.image("nope", 0, sent=False) is None

    async def test_a_batch_and_an_image_fetched_for_the_event_loop_are_what_get_and_image_read(self, pair):
        """``fetch`` and ``fetch_image`` read what ``get`` and ``image`` read, for a caller that awaits them (a
        dashboard's decision, its view of an image): once the batch is decided, nothing."""
        a, b = pair
        batch = _batch()
        batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
        await a.store(batch)
        fetched = await b.fetch(batch.id)
        assert fetched is not None and fetched.summary() == batch.summary()
        assert await b.fetch_image(batch.id, 0, sent=True) == ("c2VudA==", "image/png")
        assert await b.fetch_image(batch.id, 0, sent=False) == ("b3JpZ2luYWw=", "image/png")
        assert await b.fetch("nope") is None and await b.fetch_image(batch.id, 1, sent=False) is None
        assert a.settle(batch.id, Decision(True))
        assert await b.fetch(batch.id) is None and await b.fetch_image(batch.id, 0, sent=True) is None

    async def test_an_image_is_shown_while_its_batch_waits_and_not_once_it_is_decided(self, pair):
        a, b = pair
        batch = _batch()
        batch.images = {0: _Image("b3JpZ2luYWw=", "c2VudA==")}
        await a.store(batch)
        assert a.image(batch.id, 0, sent=True) == ("c2VudA==", "image/png")  # it waits: shown
        assert b.settle(batch.id, Decision(False, reason="you did not approve it", state="rejected"))
        assert a.image(batch.id, 0, sent=False) is None and a.image(batch.id, 0, sent=True) is None  # decided: gone

    async def test_an_answer_is_handed_over_for_its_own_batch_only(self, pair):
        a, b = pair
        first, second = _batch(polled=True), _batch(polled=True)
        await a.store(first)
        await a.store(second)
        a.settle(first.id, Decision(True))
        a.give(first.id, {"items": [{"text": "first", "count": 0}], "degraded": []})
        assert await b.answer(second.id, 0.05) is None  # the other batch still waits for you
        assert await b.answer(first.id, 5) == {"items": [{"text": "first", "count": 0}], "degraded": []}

    def test_a_change_and_how_it_ended_are_the_same_for_every_process(self, pair):
        a, b = pair
        change = Change(Config(profile=Profile.MAX), "base", "command", "claude-code", ["the profile"], ["profile"])
        a.changes[change.id] = change
        seen = b.changes.get(change.id)
        assert seen is not None and seen == change  # every field travels (E5 adds its own: who decides, who asked)
        b.settled[change.id] = "approved"  # how it ended first, then out of the queue: as Approvals settles one
        b.changes.pop(change.id, None)
        assert change.id not in a.changes and dict(a.changes) == {} and a.settled.get(change.id) == "approved"

    def test_opening_equivalent_settings_over_one_base_keeps_one_change(self, pair):
        """Break: make atomic open a blind write, or compare proposal metadata rather than settings and base."""
        a, b = pair
        first = Change(Config(profile=Profile.MAX), "base", "command", None, ["profile"], ["profile"])
        equivalent = Change(Config(profile=Profile.MAX), "base", "file", "claude-code", ["other words"], ["terms"])
        opened, superseded = a.open(first)
        assert opened == first and superseded == []
        kept, superseded = b.open(equivalent)
        assert kept == first and superseded == [] and list(a.changes) == list(b.changes) == [first.id]
        different_base = Change(Config(profile=Profile.MAX), "another base", "command", None, ["profile"], ["profile"])
        opened, superseded = b.open(different_base)
        assert opened == different_base and superseded == [first.id]
        assert a.settled[first.id] == "superseded" and list(a.changes) == [different_base.id]
        different_config = Change(
            Config(profile=Profile.SECRETS), "another base", "command", None, ["profile"], ["profile"]
        )
        opened, superseded = a.open(different_config)
        assert opened == different_config and superseded == [different_base.id]
        assert list(b.changes) == [different_config.id]

    def test_ending_a_change_reports_only_the_winning_transition(self, pair):
        """Break: report a lost update as success or overwrite the first terminal state."""
        a, b = pair
        change = Change(Config(), "base", "command", None, ["profile"], ["profile"])
        a.open(change)
        assert b.end(change.id, "rejected") is True
        assert a.end(change.id, "approved") is False
        assert b.end("never-written", "approved") is False
        assert a.settled[change.id] == "rejected" and change.id not in a.changes
        assert "never-written" not in a.settled

    def test_how_the_latest_hundred_changes_ended_is_kept_and_the_oldest_forgotten(self, pair):
        """The backend keeps the latest 100 by itself: whoever writes one more neither counts them nor picks the one
        to drop (a mapping of a server iterates in no order to pick the oldest by)."""
        a, b = pair
        for n in range(101):
            a.settled[f"change-{n}"] = "rejected" if n % 2 else "approved"
        assert "change-0" not in b.settled and b.settled.get("change-0") is None  # the oldest: forgotten
        assert [b.settled.get(f"change-{n}") for n in range(1, 101)] == ["rejected", "approved"] * 50

    def test_viewers_are_counted_for_every_process(self, pair):
        a, b = pair
        base = a.viewers()
        assert a.viewers(1) == base + 1 and b.viewers(1) == base + 2
        assert a.viewers(-1) == base + 1 and b.viewers() == base + 1


class SignInsContract:
    """``pair`` and ``clock``: two views of the sign-ins, both on the test's clock."""

    def test_a_code_works_once_and_for_two_minutes(self, pair, clock):
        a, b = pair
        code = a.issue_code("u_emilycarte")
        assert b.redeem(code) == "u_emilycarte" and a.redeem(code) is None
        late = a.issue_code("u_emilycarte")
        clock.now += CODE_TTL + 1
        assert b.redeem(late) is None and b.redeem("") is None

    def test_a_code_is_good_until_its_two_minutes_are_up(self, pair, clock):
        a, b = pair
        code = a.issue_code("u_emilycarte")
        clock.now += CODE_TTL - 1
        assert b.redeem(code) == "u_emilycarte"

    def test_a_session_lasts_twelve_hours_and_ends(self, pair, clock):
        """Twelve hours from its opening, for a session never used since. Whether its use starts them again is each
        implementation's: a computer's does (``test_local.py``), a server's does not (``tests/server/test_live.py``)."""
        a, b = pair
        first, second = a.open("u_emilycarte"), a.open("u_emilycarte")
        clock.now += SESSION_TTL - 1
        assert b.subject_of(first) == "u_emilycarte"
        b.end(first)
        assert a.subject_of(first) is None and a.subject_of("") is None
        clock.now += 2
        assert b.subject_of(second) is None

    def test_ending_a_subject_ends_its_sessions_and_codes(self, pair):
        a, b = pair
        sid, code, other = a.open("u_emilycarte"), a.issue_code("u_emilycarte"), a.open("u_danielokaf")
        b.end(subject="u_emilycarte")
        assert a.subject_of(sid) is None and a.redeem(code) is None and a.subject_of(other) == "u_danielokaf"

    def test_ending_the_empty_subject_ends_your_sessions_and_codes_and_no_one_elses(self, pair):
        a, b = pair
        sid, code, other = a.open(""), a.issue_code(""), a.open("u_emilycarte")
        b.end(subject="")  # "" is you on a computer, not "no subject": ending it ends your sessions
        assert a.subject_of(sid) is None and a.redeem(code) is None and a.subject_of(other) == "u_emilycarte"

    def test_a_computers_dashboard_signs_in_its_one_person(self, pair):
        a, b = pair
        assert b.redeem(a.issue_code("")) == "" and b.subject_of(a.open("")) == ""  # "": you, never "no one"

    def test_a_code_is_no_session_id_and_a_session_id_is_no_code(self, pair):
        a, b = pair
        code, sid = a.issue_code("u_emilycarte"), a.open("u_emilycarte")
        # The sign-in link's code is no cookie, and a cookie is not redeemed for new sessions.
        assert b.subject_of(code) is None and b.redeem(sid) is None
        assert b.redeem(code) == "u_emilycarte" and b.subject_of(sid) == "u_emilycarte"  # each still works as its own


class SettingsContract:
    @pytest.fixture(autouse=True)
    def no_port_given_from_outside(self, monkeypatch):
        """A computer's settings take their port from LATET_PORT when it is set (the container's): what these cases
        load is what they saved, whatever the environment the tests run in."""
        monkeypatch.delenv("LATET_PORT", raising=False)

    def test_settings_saved_by_one_process_are_read_by_another(self, pair):
        a, b = pair
        config = a.prepare(Config(profile=Profile.MAX, terms=[TermConfig(value="Mercury Labs", type="CLIENT")]))
        a.save(config)
        assert b.load() == config
        assert b.reviewed() is None  # saving settings approves none of them

    def test_the_settings_approved_last_are_kept_apart(self, pair):
        a, b = pair
        assert b.reviewed() is None
        approved = a.prepare(Config(history="metadata"))
        a.save_reviewed(approved)
        assert b.reviewed() == approved and b.load() != approved
        edited = a.prepare(Config(profile=Profile.MAX))  # config.yaml changes: an edit, or a change that waits for you
        a.save(edited)
        assert b.load() == edited and b.reviewed() == approved  # the approved settings are as they were

    def test_preparing_twice_changes_nothing(self, pair):
        a, _ = pair
        once = a.prepare(Config(profile=Profile.SECRETS))
        assert a.prepare(once) == once and a.prepare(once, strict=True) == once


class AwsSessionsContract:
    """``pair`` and ``clock``: two views of the sessions, both on the test's clock."""

    def test_a_sessions_credentials_last_until_they_end_or_are_dropped(self, pair, clock):
        a, b = pair
        first = AwsCredentials("AKIDEXAMPLE", "s", "t", expires=clock.now + 60)
        second = AwsCredentials("AKIDOTHER", "s2")
        ends, held = a.add_session(first), a.add_session(second)
        assert len(ends) == 32 and b.session(ends) == first and b.session(held) == second  # each its own, whole
        clock.now += 61
        assert not b.has_session(ends) and b.has_session(held)
        with pytest.raises(AwsError) as gone:
            b.session(ends)
        assert gone.value.code == "aws-session-gone"
        assert b.drop_session(held) and not a.drop_session(held) and not a.has_session(held)

    def test_a_key_that_was_never_issued_has_no_session(self, pair):
        a, b = pair
        a.add_session(AwsCredentials("AKIDEXAMPLE", "s"))  # others exist: none of them is the answer for any key
        never = "f" * 32
        assert not b.has_session(never) and not b.drop_session(never)
        with pytest.raises(AwsError) as gone:
            b.session(never)
        assert gone.value.code == "aws-session-gone"
