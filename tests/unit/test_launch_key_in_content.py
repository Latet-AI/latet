"""A live launch key found in content is a known secret (R-103; root's ruling I-6 on P2-6a's review): masked like the
bridge token where Latet rewrites the request, so an agent that ran ``env`` keeps its conversation, and the key never
reaches the provider; refused only where nothing can be masked (a request Latet only checks), with words that say how to
go on."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from latet.bridge.api import Bridge
from latet.engine import escapes
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.latet import LatetCredentialDetector
from latet.engine.response_guard import safe_text
from latet.engine.types import EntityClass, Span
from latet.engine.vault import Vault
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.routes import RouteTable, Upstream

from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire

# A key's shape (``latet_lk_`` and 43 URL-safe characters), as ``env`` prints the child's environment.
_KEY = "latet_" + "lk_" + "Qm9_" * 10 + "z1A"
_ENV = f"PATH=/usr/bin\nANTHROPIC_CUSTOM_HEADERS=x-latet-launch-key: {_KEY}\nHOME=/home/emily\n"
_CHAT_ANSWER = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'
_MESSAGES_ANSWER = b'{"content":[{"type":"text","text":"ordinary provider reply"}],"stop_reason":"end_turn"}'
_COMPANY = Upstream("company", "/company", "http://127.0.0.1:1", envelope_expected=frozenset({("model", "m")}))
_SESSION = "7e57a000-0000-4000-8000-000000000003"


def test_the_latet_detector_finds_a_launch_key():
    found = list(LatetCredentialDetector().detect(f"ok {_KEY} ok", DetectContext()))
    assert [(span.etype, f"ok {_KEY} ok"[span.start : span.end]) for span in found] == [("LATET_LAUNCH_KEY", _KEY)]
    assert not list(LatetCredentialDetector().detect("latet_lk_notes", DetectContext()))


def test_a_launch_keys_stand_in_is_never_taken_for_a_live_key():
    """The stand-in that leaves in a key's place keeps the prefix with 32 drawn characters, never a key's 43: the content
    guard, which refuses a live key's shape wherever it would leave, never refuses it."""
    for seed in range(50):
        fake = Vault(bytes([seed]) * 32).intern("LATET_LAUNCH_KEY", EntityClass.SECRET, _KEY).surrogate
        assert fake.startswith("latet_lk_") and len(fake) == len("latet_lk_") + 32 and fake != _KEY
        assert safe_text(f"x-latet-launch-key: {fake}\n"), fake


def _chat(history: int) -> bytes:
    """A Chat Completions turn whose tool ran ``env``; ``history`` later turns follow it, as the agent goes on."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Why does my build fail? Look at the environment."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": _ENV},
    ]
    for turn in range(history):
        messages += [{"role": "assistant", "content": f"Checked {turn}."}, {"role": "user", "content": "Go on."}]
    return json.dumps({"model": "m", "messages": messages}).encode()


async def _send(request: Request, upstream: Upstream, answer: bytes, tmp_path: Path) -> tuple[int, bytes, list[bytes]]:
    sent: list[bytes] = []

    def provider(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming.content)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(answer))

    with _sinks(tmp_path) as (mediator, recorder, _store):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([upstream]), Capture(tmp_path / "capture"), client, mediator, recorder)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            if isinstance(response, StreamingResponse):
                status, wire = await asyncio.wait_for(_wire(response, request), _TIMEOUT)
            else:
                status, wire = response.status_code, bytes(response.body)
        assert recorder.drain(_TIMEOUT)
    return status, wire, sent


@pytest.mark.parametrize("history", [0, 2], ids=["the-turn", "a-later-turn"])
async def test_a_launch_key_in_tool_output_is_masked_and_the_conversation_goes_on(tmp_path: Path, history: int):
    status, wire, sent = await _send(_request(_chat(history)), OPENAI, _CHAT_ANSWER, tmp_path)
    assert status == 200 and b"ordinary provider reply" in wire, wire
    assert len(sent) == 1, "one provider send"
    tool = next(m for m in json.loads(sent[0])["messages"] if m["role"] == "tool")["content"]
    assert _KEY not in sent[0].decode() and "x-latet-launch-key: " in tool and "PATH=/usr/bin" in tool


async def test_a_launch_key_on_a_request_latet_only_checks_is_refused_saying_how_to_go_on(tmp_path: Path):
    body = json.dumps(
        {
            "model": "m",
            "max_tokens": 20,
            "messages": [
                {"role": "user", "content": "Look at the environment."},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": _ENV}]},
            ],
        }
    ).encode()
    original = _request(body)
    scope = dict(original.scope)
    scope.update(path="/company/v1/messages", raw_path=b"/company/v1/messages")
    scope["headers"] = [
        *original.scope["headers"],
        (b"x-claude-code-session-id", _SESSION.encode()),
        (b"anthropic-version", b"2023-06-01"),
        (b"user-agent", b"claude-cli/2.1.312 (external, cli)"),
    ]
    status, wire, sent = await _send(Request(scope, original.receive), _COMPANY, _MESSAGES_ANSWER, tmp_path)
    words = wire.decode()
    assert 400 <= status < 500 and sent == [] and _KEY not in words
    assert "launch key" in words and "rewind" in words and "new conversation" in words, words


def test_the_plugins_mask_call_masks_a_launch_key_in_a_tool_output(tmp_path: Path):
    """Claude Code's plugin masks a tool's output before Claude Code sends it: the key leaves masked, as the bridge
    token does, and the conversation goes on."""
    asked = {"session": "s-1", "items": [{"text": _ENV, "kind": "tool_result", "tool": "Bash"}]}
    with _sinks(tmp_path) as (mediator, _recorder, _store):
        _, out, *_ = Bridge(mediator, "token")._mask(asked)
        source, reading = escapes._last
    assert _KEY not in out[0]["text"] and out[0]["count"] >= 1 and "PATH=/usr/bin" in out[0]["text"]
    assert _KEY not in source and (reading is None or _KEY not in reading.text), "the escaped reading keeps no key"


@pytest.mark.parametrize("path", ["gateway", "plugin"])
async def test_no_text_holding_a_key_reaches_the_vaults_store_lookup(tmp_path: Path, monkeypatch, path: str):
    """P2-6a's re-review, R-2: every path that masks (the gateway's slots, the engine's texts, the plugin's call) asks
    the vault for a text's values through one point, ``Vault.prefetch``, which never hands a text holding a live key to
    the store's lookup (``_prefetch``, ``_prefetch_fakes``: a server's stored vault)."""
    asked: list[str] = []

    def store_lookup(_vault: Vault, texts) -> None:
        asked.extend(texts)

    monkeypatch.setattr(Vault, "reads_store", True, raising=False)  # as a server's stored vault: it reads a store
    monkeypatch.setattr(Vault, "_prefetch", store_lookup, raising=False)
    monkeypatch.setattr(Vault, "_prefetch_fakes", store_lookup, raising=False)
    if path == "gateway":
        status, _wire_bytes, sent = await _send(_request(_chat(0)), OPENAI, _CHAT_ANSWER, tmp_path)
        assert status == 200 and len(sent) == 1
        ordinary = "Why does my build fail?"
    else:
        items = [{"text": _ENV, "kind": "tool_result", "tool": "Bash"}, {"text": "deploy it now", "kind": "user_text"}]
        with _sinks(tmp_path) as (mediator, _recorder, _store):
            _, out, *_ = Bridge(mediator, "token")._mask({"session": "s-2", "items": items})
        assert _KEY not in out[0]["text"]
        ordinary = "deploy it now"
    assert any(ordinary in text for text in asked), "an ordinary text is asked for, as before"
    assert not any(_KEY in text for text in asked), "a text holding a live key reached the store's lookup"


def test_a_stored_vault_never_asks_its_store_about_a_key_holding_texts_words():
    """N-2 (P2-6a's re-review, root's ruling): masking a text that holds a live launch key, a stored vault (a server's)
    never asks its store about that text's words, not even by their keyed prints; an ordinary text's words are asked
    for as before. The key is masked all the same, by its own detector. (The key is never looked up as a fake nor
    remembered as no fake; masked, it is kept as a value like any other, by its own keyed hash. Where the masking
    keeps fakes, each other value found is asked whether it is a fake by its own keyed hash alone:
    ``test_a_stored_vault_never_looks_up_a_live_key_by_its_keyed_hash``.)"""
    from latet.engine.pipeline import Session
    from latet.engine.types import SlotKind

    from .test_stored_vault import _email, _engine, _many, _Store, _vault

    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    _many(vault)
    store.calls.clear()
    held = engine.mask_text(f"{_ENV}owner {_email(3)}", SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert _KEY not in held.text
    assert not [call for call in store.calls if call.startswith("with_words")], store.calls
    engine.mask_text(f"owner {_email(3)}", SlotKind.TOOL_RESULT, Session("t", kept=False))
    assert [call for call in store.calls if call.startswith("with_words")], "an ordinary text is asked for, as before"


class _FoundStill:
    """A masker whose detectors of fakes find ``fakes`` too, wherever ``text`` holds one: as they find a fake memory held
    when they read the text and let go before ``Engine.adopt`` asks for it (outside a call nothing holds it, and another
    thread's call may let it go meanwhile), so that adopt asks its vault for a fake memory no longer holds."""

    def __init__(self, masker: Any, fakes: list[str]) -> None:
        self._masker, self._fakes = masker, fakes

    def surrogates_in(self, text: str) -> list[Span]:
        found = list(self._masker.surrogates_in(text))
        places = {(span.start, span.end) for span in found}
        for fake in self._fakes:
            start = text.find(fake)
            while start >= 0:
                if (start, start + len(fake)) not in places:
                    found.append(Span(start, start + len(fake), "EMAIL", EntityClass.PII, "fake"))
                start = text.find(fake, start + 1)
        return found


def _adopting(engine: Any, store: Any, monkeypatch, found_still: list[str]) -> list[str]:
    """The calls ``store`` is asked while ``engine.adopt`` runs, told apart from those of the masking around it (which
    asks whether each value it finds is a fake, ``Masker._found``), its detectors of fakes finding ``found_still``
    too (``_FoundStill``)."""
    calls: list[str] = []
    adopt = engine.adopt

    def recorded(session, text: str, masker) -> None:
        before = len(store.calls)
        adopt(session, text, _FoundStill(masker, found_still))
        calls.extend(store.calls[before:])

    monkeypatch.setattr(engine, "adopt", recorded)
    return calls


@pytest.mark.parametrize("path", ["unmask", "unmask in a call", "adopt"])
def test_a_stored_vault_never_asks_its_store_about_a_key_holding_texts_words_to_restore_or_adopt(
    path: str, monkeypatch
):
    """The security review's M-1 (N-2, the ruling's words held literally): restoring a text that holds a live launch key
    (``/unmask``: ``Engine.unmask``, ``StoredVault.candidates_in``, alone or in the call that asked for the fakes of
    every text first) and adopting the fakes a resent conversation holds (``keep_surrogates``: ``Engine.adopt``,
    ``StoredVault.fakes_out_of_memory``), a stored vault never asks its store about that text's words (``with_words``),
    as masking it never does: what memory holds is restored or adopted all the same, and an ordinary text's words are
    asked for as before. (Masking it, the key is never looked up as a fake nor remembered as no fake, and it is kept
    as a value like any other, by its own keyed hash; where the masking keeps fakes, ``keep_surrogates`` or from the
    model, each other value found is asked whether it is a fake by its own keyed hash alone, which keeps a resumed
    conversation's old fakes as fakes: ``test_a_stored_vault_never_looks_up_a_live_key_by_its_keyed_hash``.)
    Adopting, ``Engine.adopt`` asks the store nothing at all for that text, not even by the keyed hash of each whole
    fake its detectors of fakes find (``by_surrogate``, ``by_reading``), one memory let go meanwhile included: it looks
    them up in memory alone (``Vault.memory_alone``), as ``fakes_out_of_memory`` does (the minors after 0.11, item
    11)."""
    from latet.engine.pipeline import Session
    from latet.engine.types import SlotKind

    from .test_stored_vault import _email, _engine, _held, _many, _Store, _vault

    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    _many(vault)
    gone = next(e for e in store.rows.values() if e.original == _email(3))
    kept = next(e for e in store.rows.values() if e.original == _email(499))
    assert not _held(vault, gone) and _held(vault, kept)
    text = f"{_ENV}owners {gone.surrogate} and {kept.surrogate}"
    ordinary = f"owner {gone.surrogate}"
    store.calls.clear()
    if path == "adopt":
        other, lost = (next(e for e in store.rows.values() if e.original == _email(n)) for n in (4, 5))
        assert not _held(vault, other) and not _held(vault, lost)
        store.forget_entity(lost.id)  # forgotten meanwhile: its fake, still found, is the store's no more
        adopting = _adopting(engine, store, monkeypatch, found_still=[gone.surrogate, other.surrogate, lost.surrogate])
        session = Session("t", kept=False)
        first = engine.mask_text(f"{text} and {lost.surrogate}", SlotKind.ASSISTANT_TEXT, session, keep_surrogates=True)
        assert adopting == []  # adopt asked nothing at all: by_surrogate and by_reading included
        assert not [call for call in store.calls if call.startswith("with_words")], store.calls
        assert kept.id in session.entity_ids and gone.id not in session.entity_ids  # memory's adopted alone
        # Adopt's lookup in memory alone remembers no miss: the masking then asks the store whether gone's fake is one,
        # and it leaves as the fake the model saw, never kept as a value of its own.
        assert gone.surrogate in first.text and not [e for e in store.rows.values() if e.original == gone.surrogate]
        engine.mask_text(f"owner {other.surrogate}", SlotKind.ASSISTANT_TEXT, session, keep_surrogates=True)
        assert other.id in session.entity_ids and "by_surrogate" in adopting  # an ordinary text's: asked, as before
        engine.mask_text(ordinary, SlotKind.ASSISTANT_TEXT, session, keep_surrogates=True)
        assert gone.id in session.entity_ids  # an ordinary text's fake is adopted, as before
    else:
        with vault.holding():
            if path == "unmask in a call":  # as the applications' /unmask asks: every text's fakes first
                vault.prefetch_fakes([text, f"owner {_email(7)}"])
                store.calls.clear()
            restored, _ = engine.unmask(text)
        assert not [call for call in store.calls if call.startswith("with_words")], store.calls
        assert kept.original in restored and gone.surrogate in restored  # memory's restored alone
        assert engine.unmask(ordinary)[0] == f"owner {gone.original}"  # asked for, as before
    assert [call for call in store.calls if call.startswith("with_words")], "an ordinary text is asked for, as before"


@pytest.mark.parametrize(
    "path", ["masked with keep_surrogates", "masked from the model", "checked for values in clear"]
)
def test_a_stored_vault_never_looks_up_a_live_key_by_its_keyed_hash(path: str):
    """Root's ruling on Task 5's review (its question 2, option c): a stored vault looks up a string that holds a live
    launch key in memory alone, whoever asks (``StoredVault.by_surrogate``). The masking's check of whether each value
    it finds is a fake (``Masker._found``: with ``keep_surrogates``, ``Engine.adopt`` included, or from the model) and
    the check of a text for values in clear (``known_outside``, ``fake_or_let_through``: a conversation's document that
    leaves as it left) never look the key up in the store as a fake, not even by its keyed hash, and the key is never
    remembered as a string that is no fake (``_misses``); masked, it is kept as a value like any other, by its own
    keyed hash. No fake that ever left Latet has a key's shape (a stand-in has 32 characters after the prefix, never
    43), so nothing is lost: an old fake out of memory in the same text is still asked for by its own keyed hash,
    leaves as the fake the model saw and is restored to its value."""
    from latet.engine.pipeline import Session
    from latet.engine.types import SlotKind

    from .test_stored_vault import _email, _engine, _held, _many, _Store, _vault

    store = _Store()
    vault = _vault(store)
    engine = _engine(vault)
    _many(vault)
    gone = next(e for e in store.rows.values() if e.original == _email(3))
    assert not _held(vault, gone)
    asked: list[str] = []
    for name in ("by_surrogate", "by_reading", "by_original"):
        real = getattr(store, name)
        setattr(store, name, lambda value, real=real: (asked.append(value), real(value))[1])
    session = Session("t", kept=False)
    first = engine.mask_text(f"{_ENV}owner {_email(7)}", SlotKind.TOOL_RESULT, session)  # the key kept as a value
    assert _KEY not in first.text
    text = f"{_ENV}owners {gone.surrogate}"
    if path == "checked for values in clear":
        with vault.holding():
            assert engine.masker.known_outside(f"{_ENV}owner {_email(7)}", ())  # the key and the address, in clear
    else:
        if path == "masked with keep_surrogates":
            out = engine.mask_text(text, SlotKind.TOOL_RESULT, session, keep_surrogates=True).text
        else:
            out = engine.mask_text(text, SlotKind.ASSISTANT_TEXT, session, from_model=True).text
        assert _KEY not in out
        assert gone.surrogate in out and engine.unmask(out)[0].endswith(gone.original)  # an old fake stays a fake
        assert gone.surrogate in asked  # the text's other values are still asked whether they are fakes
    assert not [value for value in asked if _KEY in value], asked  # never by the key's keyed hash
    assert not [miss for miss in vault._misses if _KEY in miss]  # never kept as a string that is no fake


def test_the_verify_only_test_is_written_once():
    """P2-6a's re-review, R-4: whether a request is one Latet only checks decides both whether a live key in its texts
    is screened before masking and how it is prepared; one helper answers both, so they cannot drift."""
    import ast
    import inspect

    from latet.gateway import mediate

    tree = ast.parse(inspect.getsource(mediate))
    subscription = [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "sk-ant-oat"]
    assert len(subscription) == 1, "the verify-only test is written more than once"
    helper = mediate.verify_only
    assert "sk-ant-oat" in inspect.getsource(helper)
    prepare = inspect.getsource(mediate.Mediator._prepare)
    assert prepare.count("verify_only(") == 1, "the preparation asks the helper once and uses its answer for both"
