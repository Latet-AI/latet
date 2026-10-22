import asyncio
import itertools
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import zstandard

from latet.engine.types import EntityClass, SlotKind
from latet.engine.verifier import Leak
from latet.gateway.mediate import VERIFY, Prepared
from latet.gateway.recorder import Outcome, Recorder, reconstruct
from latet.protocols.base import SlotRef
from latet.storage.exchanges import BlockRecord, ExchangeRecord, ExchangeStore, ImageRecord

from ..conftest import ROOT_KEY

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"


def _summary(ts, session="s", flags=(), agent="codex"):
    return {"ts": ts, "session": session, "agent": agent, "flags": list(flags), "new_masked": 1}


def _block(store, text, new=True, stored=False):
    return BlockRecord(store.block_id(text), "$.input[0]", "user_text", False, new, None if stored else text, [])


def test_blocks_are_stored_once_and_sealed(tmp_path):
    path = tmp_path / "x.db"
    store = ExchangeStore(path, ROOT_KEY)
    marker = "CANARY-TEXT-1234567890"
    shared = _block(store, f"history {marker}")
    a = store.record(ExchangeRecord(_summary(time.time()), [shared, _block(store, "turn one")]))
    b = store.record(
        ExchangeRecord(_summary(time.time()), [_block(store, f"history {marker}", new=False, stored=True)])
    )
    assert store._db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 2
    assert [s["masked"] for s in store.detail(b)["request"]["slots"]] == [f"history {marker}"]
    assert len(store.detail(a)["request"]["slots"]) == 2
    store.close()
    assert marker.encode() not in path.read_bytes()  # encrypted at rest


def test_pagination_and_filters(tmp_path):
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    now = time.time()
    ids = [store.record(ExchangeRecord(_summary(now + i, flags=["blocked"] if i == 3 else []))) for i in range(5)]
    page, cursor = store.list(limit=2)
    assert [p["id"] for p in page] == [ids[4], ids[3]]
    page2, cursor2 = store.list(limit=2, before=cursor)
    assert [p["id"] for p in page2] == [ids[2], ids[1]]
    page3, cursor3 = store.list(limit=2, before=cursor2)
    assert [p["id"] for p in page3] == [ids[0]] and cursor3 is None
    assert [p["id"] for p in store.list(flag="blocked")[0]] == [ids[3]]
    assert store.list(agent="claude-code")[0] == []
    [session] = store.sessions()
    assert session["exchanges"] == 5 and session["blocked"] == 1 and session["masked"] == 5


def test_a_conversation_s_exchanges_come_in_the_order_they_were_recorded(tmp_path):
    """Its requests are sent one after the other: recorded in that order, though they can start in the same tick
    of the clock (15.6 ms on Windows) and their ids sort at random then."""
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    now = time.time()
    ids = [store.record(ExchangeRecord(_summary(now, session="conv", agent="tutorial-agent"))) for _ in range(6)]
    store.record(ExchangeRecord(_summary(now, session="other")))
    later = store.record(ExchangeRecord(_summary(now - 5, session="conv")))  # a clock set back meanwhile
    assert store.in_order("conv") == [*ids, later]
    assert store.in_order("nobody") == []


def test_prune_keeps_blocks_a_recent_exchange_carried(tmp_path):
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY, retention_days=14)
    old = time.time() - 20 * 86400
    store.record(ExchangeRecord(_summary(old), [_block(store, "old only"), _block(store, "shared")]))
    store._db.execute("UPDATE blocks SET last_seen = ?", (old,))
    store.record(ExchangeRecord(_summary(time.time()), [_block(store, "shared", stored=True)]))
    assert store.prune() == 1
    assert store._db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
    [summary] = store.list()[0]
    assert store.detail(summary["id"])["request"]["slots"][0]["masked"] == "shared"


def test_reconstruct_keeps_unknown_values_masked(vault):
    entity = vault.intern("EMAIL", EntityClass.PII, "a.b@acme.it")
    masked = f"mail {entity.surrogate} or [REDACTED:TOKEN]"
    start = masked.index(entity.surrogate)
    redacted = masked.index("[REDACTED")
    repl = [
        {"masked_start": start, "masked_end": start + len(entity.surrogate), "type": "EMAIL", "entity": entity.id},
        {"masked_start": redacted, "masked_end": len(masked), "type": "TOKEN", "entity": None},
    ]
    original, spans = reconstruct(masked, repl, vault)
    assert original == "mail a.b@acme.it or [REDACTED:TOKEN]"
    assert [s["known"] for s in spans] == [True, False]
    assert original[spans[0]["start"] : spans[0]["end"]] == "a.b@acme.it"


def test_a_range_of_the_masked_text_is_found_in_the_original(vault):
    from latet.gateway.recorder import original_range

    mail = vault.intern("EMAIL", EntityClass.PII, "a.b@acme.it")
    name = vault.intern("PERSON", EntityClass.IDENTITY, "Marco Ferretti")
    masked = f"to {mail.surrogate} from {name.surrogate} with {GH_PAT}!"
    repl = [
        {"masked_start": masked.index(x.surrogate), "masked_end": masked.index(x.surrogate) + len(x.surrogate)}
        | {"type": etype, "entity": x.id}
        for x, etype in ((mail, "EMAIL"), (name, "PERSON"))
    ]
    original, spans = reconstruct(masked, repl, vault)
    start = masked.index(GH_PAT)
    assert start != original.index(GH_PAT)  # the values masked before it moved it
    where = original_range(spans, start, start + len(GH_PAT))
    assert original[where[0] : where[1]] == GH_PAT
    assert original_range(spans, 0, 2) == (0, 2)  # before every value: where it was
    # A range that cuts into a masked value takes the whole value in.
    inside = masked.index(mail.surrogate) + 2
    cut = original_range(spans, inside, start + 4)
    assert original[cut[0] : cut[1]] == f"a.b@acme.it from Marco Ferretti with {GH_PAT[:4]}"
    # A value the vault forgot stays as it was sent: it moves nothing, the others still do.
    vault.forget(mail.id)
    lost, spans = reconstruct(masked, repl, vault)
    where = original_range(spans, start, start + len(GH_PAT))
    assert lost[where[0] : where[1]] == GH_PAT


def _stopped(recorder, texts, leaks):
    """The record of a request Latet stopped, as the gateway and the plugin's final check leave it."""
    pending = [(SlotRef(texts, i, SlotKind.TOOL_RESULT), [], False) for i in range(len(texts))]
    prepared = Prepared(VERIFY, "anthropic", b"", None, leaks, document=texts, pending=pending, block_reason="stopped")
    outcome = Outcome({"user-agent": "claude-cli/2"}, "anthropic", "/v1/messages", "http", time.time(), prepared)
    recorder.finish(outcome)
    assert recorder.drain()
    [summary] = recorder.store.list()[0]
    return recorder.store.detail(summary["id"])


def test_a_stopped_value_is_recorded_with_its_block_and_where_in_it(tmp_path):
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    texts = ["", f"deploy with {GH_PAT} now", "and then stop"]  # the empty one is not a block of the record
    start = texts[1].index(GH_PAT)
    leak = Leak(
        "GITHUB_PAT", EntityClass.SECRET, "gitleaks:github-pat", SlotKind.TOOL_RESULT, "GITHUB_PAT[40]", True,
        slot=1, start=start, end=start + len(GH_PAT),
    )  # fmt: skip
    detail = _stopped(Recorder(store), texts, [leak])
    [stored] = detail["leaks"]
    [slot] = [s for s in detail["request"]["slots"] if s["block"] == stored["block"]]
    assert slot["masked"] == texts[1]
    assert slot["masked"][stored["masked_start"] : stored["masked_end"]] == GH_PAT
    assert (stored["class"], stored["rule"]) == ("SECRET", "gitleaks:github-pat")
    assert (stored["type"], stored["preview"], stored["new"]) == ("GITHUB_PAT", "GITHUB_PAT[40]", True)
    assert GH_PAT not in json.dumps(stored)  # where the value is, never what it is
    # The history keeping no texts has nothing to point at: the leak is only its type and length.
    other = ExchangeStore(tmp_path / "y.db", ROOT_KEY)
    [bare] = _stopped(Recorder(other, history="metadata"), texts, [leak])["leaks"]
    assert bare == {"type": "GITHUB_PAT", "kind": "tool_result", "preview": "GITHUB_PAT[40]", "new": True}


def test_what_stopped_a_request_names_a_field_by_its_kind_and_one_kept_before_is_cut_once_the_history_opens(tmp_path):
    """A computer's history keeps what stopped a request in clear, and the rule that found a value by its field names
    the field, whose name can be a value (an address before a secret's word): the field is named by its kind alone."""
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    texts = ["", "emily.carter@acme.com password: hunter2222"]
    start = texts[1].index("hunter2222")
    leak = Leak(
        "PASSWORD", EntityClass.SECRET, "field-secret:emily.carter@acme.com password", SlotKind.TOOL_RESULT,
        "PASSWORD[10]", True, slot=1, start=start, end=start + 10,
    )  # fmt: skip
    [stored] = _stopped(Recorder(store), texts, [leak])["leaks"]
    assert stored["rule"] == "field-secret:password"
    [(raw,)] = store._db.execute("SELECT leaks FROM exchanges").fetchall()
    assert "emily" not in raw and "carter" not in raw
    named = json.dumps([stored | {"rule": "field-secret:emily.carter@acme.com password"}])
    store._db.execute("UPDATE exchanges SET leaks = ?", (named,))  # kept by an earlier Latet
    store.close()
    again = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    [(raw,)] = again._db.execute("SELECT leaks FROM exchanges").fetchall()
    assert json.loads(raw)[0]["rule"] == "field-secret:password" and "emily" not in raw


def test_live_feed_receives_summaries_and_recording_never_raises(tmp_path):
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    recorder = Recorder(store)

    async def scenario():
        queue = recorder.broadcaster.subscribe()
        recorder.finish(
            Outcome({"user-agent": "curl/8"}, "openai", "/openai/v1/models", "http", time.time(), status=200)
        )
        recorder.finish(object())  # a broken outcome: logged, never raised
        return await asyncio.wait_for(queue.get(), timeout=10)

    summary = asyncio.run(scenario())
    assert summary["mode"] == "passthrough" and "unknown-route" in summary["flags"]
    assert summary["verified"] is False
    recorder.close()
    assert len(store.list()[0]) == 1


def test_no_decompressor_of_the_history_is_used_by_two_threads_at_once(tmp_path, monkeypatch):
    """zstandard's decompressors are not for two threads at once, and the recorder's thread and the dashboard's
    workers use one store together: ``image()`` read one decompressor outside the store's lock, shared by every
    reading. Each opening has a decompressor of its own."""
    used_by: dict[int, set[int]] = {}
    real, numbers = zstandard.ZstdDecompressor, itertools.count()

    class Watched:
        def __init__(self, *args, **kwargs):
            self._inner, self._number = real(*args, **kwargs), next(numbers)  # an id() is used again once freed

        def decompress(self, data, *args, **kwargs):
            used_by.setdefault(self._number, set()).add(threading.get_ident())
            return self._inner.decompress(data, *args, **kwargs)

    monkeypatch.setattr(zstandard, "ZstdDecompressor", Watched)
    store = ExchangeStore(tmp_path / "x.db", ROOT_KEY)
    image = ImageRecord("$", "masked", store.image_id("aGk="), "image/png", "aGk=")
    exchange = store.record(ExchangeRecord(_summary(time.time()), images=[image]))
    with ThreadPoolExecutor(4) as pool:
        found = list(pool.map(lambda n: store.image(image.id) if n % 2 else store.detail(exchange), range(40)))
    assert found[1] == ("image/png", b"hi") and found[0] is not None
    assert used_by and all(len(threads) == 1 for threads in used_by.values())
    store.close()
