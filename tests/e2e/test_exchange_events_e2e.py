"""Each exchange reaches the edition as one event, whatever the history keeps (full, metadata or off) and when the
recorder is too far behind to count it; it names the identity a server's space is for; and an edition that keeps no
events costs nothing more."""

import gzip
import json
import logging
import re
import subprocess
import threading
from collections import Counter
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig, TermConfig
from latet.edition import Community
from latet.engine.masker import Masker
from latet.engine.vault import MaskingError
from latet.gateway import recorder as recorder_module
from latet.gateway.app import create_app
from latet.gateway.aws import AwsCredentialStore, profile_tag
from latet.gateway.cloud import BedrockUpstream
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "emily.carter@acme.com"
# Model ids as agents send them (0.11 reads a call's model as its profile names it: never `m`).
GPT, CLAUDE_MODEL = "gpt-4.1-mini", "claude-sonnet-4-5"
AUTH = {"authorization": "Bearer sk-test"}
CLAUDE = {
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": "5e8d2c1a-7b4f-4e09-9c3d-2a6f8b1e4d70",
}
EMILY = SimpleNamespace(id="u_emilycarte", name="Emily Carter", kind="person")


class _Recording:
    """An edition that keeps every event in a list (or keeps none, with ``keeps`` false); its other hooks are the
    Community Edition's."""

    def __init__(self, keeps: bool = True) -> None:
        self._community = Community()
        self.keeps = keeps
        self.events: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._community, name)

    def record(self, event):
        self.events.append(dict(event))

    def keeps_events(self) -> bool:
        return self.keeps


@pytest.fixture(scope="module")
def upstream():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def runtimes(tmp_path, upstream):
    made = []

    def make(history: str, keeps: bool = True, terms: tuple[TermConfig, ...] = ()):
        edition = _Recording(keeps)
        root = tmp_path / f"{history}-{len(made)}"
        config = Config(identity=IdentityConfig(enabled=False), history=history, terms=list(terms))
        rt = make_runtime(config, data=root / "data", config_file=root / "config.yaml", edition=edition)
        rt.gateway.upstreams = [
            Upstream("openai", "/openai", upstream.url),
            Upstream("anthropic", "/anthropic", upstream.url),
        ]
        made.append(rt)
        return rt, edition

    yield make
    for rt in made:
        rt.close()


def _morning(rt, root) -> None:
    """A conversation that grows turn by turn (its history sent again each time), one in a project, one stopped."""
    project = root / "billing-api"
    project.mkdir(exist_ok=True)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        history: list[dict] = []
        for turn in (f"mail {EMAIL} about the invoice", "and attach the receipt", "thanks"):
            body = {"model": GPT, "messages": [*history, {"role": "user", "content": turn}]}
            assert client.post("/openai/v1/chat/completions", json=body, headers=AUTH).status_code == 200
            history += [{"role": "user", "content": turn}, {"role": "assistant", "content": "ok"}]
        body = {"model": GPT, "messages": [{"role": "user", "content": f"deploy with {GH_PAT}"}]}
        headers = AUTH | {"x-latet-project": str(project)}
        assert client.post("/openai/v1/chat/completions", json=body, headers=headers).status_code == 200
        stop = {"model": CLAUDE_MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": f"key {GH_PAT}"}]}
        assert client.post("/anthropic/v1/messages", json=stop, headers=CLAUDE).status_code == 400
    assert rt.recorder.drain()


def _brief(event):
    return (event["type"], event["masked"], event["stopped"], event["reason"], event["project"])


def test_each_exchange_is_one_event_that_adds_up_to_the_history(runtimes, tmp_path):
    rt, edition = runtimes("full")
    _morning(rt, tmp_path)
    summaries = rt.exchanges.summaries_since(0)
    assert len(edition.events) == len(summaries) == 5
    assert [e["type"] for e in edition.events].count("stopped") == sum("blocked" in s["flags"] for s in summaries) == 1
    totals, expected = Counter(), Counter()
    for event in edition.events:
        totals.update(event["masked"])
    for summary in summaries:
        expected.update(summary["new_by_type"])
    assert totals == +expected and totals["EMAIL"] == 1  # sent three times, counted once
    assert [e["project"] for e in edition.events][3] == "billing-api"
    assert [e["reason"] for e in edition.events if e["type"] == "stopped"] == ["leak"]


def test_an_event_names_the_identity_its_space_is_for(runtimes, tmp_path):
    rt, edition = runtimes("full")
    rt.identity = EMILY  # as a server's space is made for its identity (E0a)
    _morning(rt, tmp_path)
    assert {(e["identity"], e["kind"]) for e in edition.events} == {("u_emilycarte", "person")}


@pytest.mark.parametrize("history", ["metadata", "off"])
def test_the_events_do_not_depend_on_what_the_history_keeps(runtimes, tmp_path, history):
    full_rt, full = runtimes("full")
    other_rt, other = runtimes(history)
    _morning(full_rt, tmp_path)
    queue = other_rt.recorder.broadcaster.subscribe()
    _morning(other_rt, tmp_path)
    assert len(full.events) == 5
    assert [_brief(e) for e in other.events] == [_brief(e) for e in full.events]
    if history == "off":
        assert other_rt.exchanges.summaries_since(0) == [] and queue.empty()


def test_an_exchange_is_recorded_as_the_history_was_when_it_ended(runtimes, tmp_path):
    """The history is read once, where the exchange ends: a change of the settings while it waits to be recorded never
    makes a record half of one history and half of the other."""
    rt, edition = runtimes("full")
    busy = threading.Event()
    rt.recorder._executor.submit(busy.wait, 10)  # the recorder's thread busy: the exchange waits behind it
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        body = {"model": GPT, "messages": [{"role": "user", "content": f"mail {EMAIL} about the invoice"}]}
        assert client.post("/openai/v1/chat/completions", json=body, headers=AUTH).status_code == 200
        stop = {"model": CLAUDE_MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": f"key {GH_PAT}"}]}
        assert client.post("/anthropic/v1/messages", json=stop, headers=CLAUDE).status_code == 400
        rt.recorder.history = "metadata"  # the settings changed meanwhile
        busy.set()
        assert rt.recorder.drain()
    masked, stopped = (rt.exchanges.detail(s["id"]) for s in rt.exchanges.summaries_since(0))
    assert masked is not None and masked["request"]["slots"] and masked["response"] is not None  # a full record
    assert stopped is not None and stopped["leaks"] and "block" in stopped["leaks"][0]  # its leak placed in its block
    assert [e["masked"] for e in edition.events] == [{"EMAIL": 1}, {}]


@pytest.mark.parametrize("history", ["full", "off"])
def test_a_term_whose_type_an_event_cannot_name_is_counted_under_its_code(runtimes, history):
    """A term's type is the label a person gave it: one an event cannot name as it is (a space, a dash, an accent) is
    counted under its code, never lost to an uncounted event."""
    terms = (
        TermConfig(value="Contoso", type="client name"),
        TermConfig(value="Fabrikam", type="Client-Name"),
        TermConfig(value="Northwind", type="Kunde_\N{LATIN CAPITAL LETTER A WITH DIAERESIS}"),
    )
    rt, edition = runtimes(history, terms=terms)
    text = "Contoso and Fabrikam, then Northwind"
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        body = {"model": GPT, "messages": [{"role": "user", "content": text}]}
        assert client.post("/openai/v1/chat/completions", json=body, headers=AUTH).status_code == 200
    assert rt.recorder.drain()
    [event] = edition.events
    assert (event["reason"], event["masked"]) == (None, {"CLIENT_NAME": 2, "KUNDE_A": 1})


# What a project hidden for a term is named by: the edition's answer. The Community Edition's, which a recording
# edition gives (it keeps no events, so no key is at hand), is one fixed code.
HIDDEN = re.compile(r"masked(?:-[a-z2-7]{8})?")


def _asked(rt, folder, content="the quarterly numbers"):
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        body = {"model": GPT, "messages": [{"role": "user", "content": content}]}
        headers = AUTH | {"x-latet-project": str(folder)}
        assert client.post("/openai/v1/chat/completions", json=body, headers=headers).status_code == 200
    assert rt.recorder.drain()


@pytest.mark.parametrize("history", ["full", "off"])
def test_a_project_whose_name_holds_a_term_the_space_masks_is_hidden(runtimes, tmp_path, history):
    """L3 of the final security review: the provider never reads the folder's name of a codename the space masks, and
    neither do the reports nor a SIEM: the project goes there as the edition names a hidden one. A name that holds
    none goes as it is."""
    rt, edition = runtimes(history, terms=(TermConfig(value="Falcon", type="client"),))
    secret, plain = tmp_path / "falcon-acme-merger", tmp_path / "billing-api"
    for folder in (secret, plain):
        folder.mkdir()
        _asked(rt, folder)
    projects = [e["project"] for e in edition.events]
    assert projects == ["masked", "billing-api"]
    assert "falcon" not in json.dumps(edition.events).lower()


def test_a_hidden_project_has_one_name_for_every_person_and_folder_the_editions(runtimes, tmp_path):
    """R3 of the re-review: a project hidden for a term is named by what the edition makes of its name (on a server,
    keyed with the server's events key), the same for every person and every folder of that name: never its folder's
    path or a hash of it, which differs for each person and each clone and follows one person's work."""
    asked, values = [], []
    for person in ("alice", "bob"):
        rt, edition = runtimes("full", terms=(TermConfig(value="Falcon", type="client"),))
        edition.hidden_project = lambda name: asked.append(name) or "masked-abcdefgh"
        folder = tmp_path / person / "work" / "falcon-acme-merger"
        folder.mkdir(parents=True)
        _asked(rt, folder)
        [event] = edition.events
        values.append(event["project"])
    assert values == ["masked-abcdefgh", "masked-abcdefgh"] and asked == ["falcon-acme-merger", "falcon-acme-merger"]


@pytest.mark.parametrize("answer", ["falcon-acme-merger", "3fa9c0d1e2b4", "masked-ABCDEFGH", None, 7, RuntimeError])
def test_an_answer_of_the_edition_that_is_not_a_hidden_name_is_never_sent(runtimes, tmp_path, answer, caplog):
    """R3: the edition's answer goes only when it has a hidden project's shape (`masked`, or `masked-` and 8 base32
    characters); anything else, the name itself, an id, or an edition that raises, goes as `masked`, the error said by
    its type and never its words."""
    rt, edition = runtimes("full", terms=(TermConfig(value="Falcon", type="client"),))

    def hidden(name):
        if answer is RuntimeError:
            raise RuntimeError("Falcon")
        return answer

    edition.hidden_project = hidden
    folder = tmp_path / "falcon-acme-merger"
    folder.mkdir()
    with caplog.at_level(logging.ERROR, logger="latet.runtime"):
        _asked(rt, folder)
    [event] = edition.events
    assert event["project"] == "masked" and "Falcon" not in caplog.text


def test_a_term_of_the_projects_own_rules_hides_it_too(runtimes, tmp_path):
    """L3: a term the project's own latet.yaml masks is one the space masks there."""
    rt, edition = runtimes("full")
    folder = tmp_path / "falcon-acme-merger"
    (folder / ".git").mkdir(parents=True)
    (folder / "latet.yaml").write_text("terms: [{value: Falcon, type: client}]\n", "utf-8")
    _asked(rt, folder, "Falcon, the quarterly numbers")
    [event] = edition.events
    assert event["masked"] == {"CLIENT": 1} and event["project"] == "masked"


def test_a_project_whose_name_cannot_be_weighed_is_hidden(runtimes, tmp_path, monkeypatch, caplog):
    """L3: a name the terms could not be read for is hidden, never sent in clear, said by the error's type."""
    import latet.runtime as runtime_module

    def broken(name, maskers):
        raise RuntimeError("Falcon")  # its words are never said

    monkeypatch.setattr(runtime_module, "holds_a_term", broken)
    rt, edition = runtimes("full")
    folder = tmp_path / "billing-api"
    folder.mkdir()
    with caplog.at_level(logging.ERROR, logger="latet.runtime"):
        _asked(rt, folder)
    [event] = edition.events
    assert HIDDEN.fullmatch(event["project"] or "")
    assert "RuntimeError" in caplog.text and "Falcon" not in caplog.text


def test_the_terms_of_a_company_hide_a_project_and_no_other_detector_is_read():
    """L3: the company's locked terms and the terms it syncs are terms the space masks; a project's name is weighed for
    the terms alone (a detector of another kind is never asked)."""
    from latet.engine.detectors.terms import LiveTerms, LockedTermsDetector, Term
    from latet.runtime import holds_a_term

    class Asked:
        id = "pii"

        def detect(self, text, ctx):
            raise AssertionError("only the term detectors are read")

    locked = SimpleNamespace(detectors=[Asked(), LockedTermsDetector([Term("Falcon", "CLIENT")])])
    synced = LiveTerms()
    synced.use([("Northwind", "client")], "tag-1")
    company = SimpleNamespace(detectors=[synced])
    assert holds_a_term("falcon-acme-merger", [locked]) and holds_a_term("northwind-audit", [company])
    assert not holds_a_term("billing-api", [locked, company])


def test_with_the_history_off_an_edition_that_keeps_no_events_costs_nothing(runtimes, tmp_path):
    rt, edition = runtimes("off", keeps=False)
    counted: list[int] = []
    for name in ("_record", "_counted"):
        original = getattr(rt.recorder, name)
        setattr(rt.recorder, name, lambda *args, original=original: counted.append(1) or original(*args))
    _morning(rt, tmp_path)
    assert counted == [] and edition.events == []


def test_with_the_history_off_the_recorder_reads_nothing_of_the_vault(runtimes, tmp_path, monkeypatch):
    full_rt, full = runtimes("full")
    _morning(full_rt, tmp_path)
    rt, edition = runtimes("off")

    def never(*args, **kwargs):
        raise AssertionError("with the history off the recorder read the conversation's values")

    # What reads the vault on the recorder's side: the conversation's restores (every value of it, I9), the annotator
    # built over them, and the replacements' rules looked up in the vault. The gateway's own restores are not these.
    monkeypatch.setattr(rt.recorder, "_snapshot", never)
    monkeypatch.setattr(rt.recorder, "_annotator", never)
    monkeypatch.setattr(recorder_module, "replacement_dicts", never)
    monkeypatch.setattr(recorder_module.ResponseCapture, "settle", never)  # the answer, as the model wrote it
    _morning(rt, tmp_path)
    assert len(full.events) == 5 and any(e["masked"] for e in full.events)
    assert [_brief(e) for e in edition.events] == [_brief(e) for e in full.events]  # the same counts, from the slots


def test_an_exchange_the_recorder_is_too_far_behind_to_count_is_an_event_all_the_same(runtimes, tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_module, "_MAX_QUEUED", 0)
    rt, edition = runtimes("full")
    _morning(rt, tmp_path)
    assert len(edition.events) == 5 and rt.exchanges.summaries_since(0) == []
    assert {e["reason"] for e in edition.events if e["type"] == "exchange"} == {"uncounted"}
    assert [e["reason"] for e in edition.events if e["type"] == "stopped"] == ["leak"]
    assert all(not e["masked"] for e in edition.events)


@pytest.mark.parametrize("history", ["full", "off"])
def test_an_exchange_the_recorder_fails_to_count_is_an_event_all_the_same(runtimes, tmp_path, history):
    rt, edition = runtimes(history)
    name = "_record" if history == "full" else "_counted"

    def failing(*args):
        raise RuntimeError("the history's disk is full")

    setattr(rt.recorder, name, failing)
    _morning(rt, tmp_path)
    assert len(edition.events) == 5 and rt.exchanges.summaries_since(0) == []
    assert {e["reason"] for e in edition.events if e["type"] == "exchange"} == {"uncounted"}
    assert [e["reason"] for e in edition.events if e["type"] == "stopped"] == ["leak"]


def test_an_exchange_the_recorder_cannot_queue_is_an_event_all_the_same(runtimes, tmp_path, monkeypatch):
    rt, edition = runtimes("full")

    def failing(*args):
        raise RuntimeError("the vault's store did not answer")

    monkeypatch.setattr(rt.recorder, "_snapshot", failing)
    _morning(rt, tmp_path)
    assert len(edition.events) == 5 and rt.exchanges.summaries_since(0) == []
    assert {e["reason"] for e in edition.events if e["type"] == "exchange"} == {"uncounted"}
    assert [e["reason"] for e in edition.events if e["type"] == "stopped"] == ["leak"]


def test_an_exchange_whose_event_cannot_be_made_at_all_never_breaks_the_traffic(
    runtimes, tmp_path, caplog, monkeypatch
):
    rt, edition = runtimes("full")

    def failing(*args):
        raise RuntimeError(f"what it carried: {EMAIL}")

    monkeypatch.setattr(rt.recorder, "_snapshot", failing)
    monkeypatch.setattr(recorder_module, "flags_of", failing)
    with caplog.at_level(logging.ERROR):
        _morning(rt, tmp_path)  # every request answered as it would be
    assert edition.events == []
    assert "could not hand an exchange's event over: RuntimeError" in caplog.text and EMAIL not in caplog.text


def test_a_summary_the_events_cannot_read_is_its_exchanges_event_all_the_same_uncounted(runtimes, caplog):
    """A count the event's schema does not take (one that is no number) never loses the exchange's event: it goes
    uncounted, said in the log by the error's type alone, never by its words (which quote what it was given)."""
    rt, edition = runtimes("full")
    summary = {
        "ts": 1_790_000_000.0, "agent": "claude-code", "path": "/anthropic/v1/messages", "signed": None,
        "flags": ["blocked", "leak"], "block_reason": "sensitive values would leave in clear: EMAIL[21]",
        "new_by_type": {"EMAIL": "two of them"}, "project": {"id": "p1", "name": "billing-api"},
    }  # fmt: skip
    with caplog.at_level(logging.ERROR):
        assert rt.recorder.on_summary is not None
        rt.recorder.on_summary(summary)
    [event] = edition.events
    assert (event["type"], event["reason"], event["masked"], event["project"]) == ("stopped", "leak", {}, "billing-api")
    rt.recorder.on_summary(summary | {"flags": []})
    assert _brief(edition.events[1]) == ("exchange", {}, 0, "uncounted", "billing-api")
    assert "ValueError" in caplog.text and "two of them" not in caplog.text


def test_an_edition_that_does_not_take_an_event_never_breaks_the_traffic(runtimes, tmp_path, caplog):
    rt, edition = runtimes("full")

    def refusing(event):
        raise RuntimeError(f"the edition is closing: {event}")

    edition.record = refusing
    with caplog.at_level(logging.ERROR):
        _morning(rt, tmp_path)
    assert len(rt.exchanges.summaries_since(0)) == 5  # the history keeps every exchange all the same
    assert "could not hand an exchange's event over: RuntimeError" in caplog.text
    assert EMAIL not in caplog.text and "the edition is closing" not in caplog.text


def test_an_edition_that_cannot_say_whether_it_keeps_events_is_handed_each_one(runtimes, tmp_path, caplog):
    rt, edition = runtimes("off")

    def unsure():
        raise RuntimeError("the license file is being read")

    edition.keeps_events = unsure
    with caplog.at_level(logging.ERROR):
        _morning(rt, tmp_path)  # every request answered as it would be
    assert len(edition.events) == 5 and rt.exchanges.summaries_since(0) == []
    assert "RuntimeError" in caplog.text and "the license file" not in caplog.text


# --- the gateway's own stops, end to end: their words and the event's code cannot drift apart unseen ----------------


class _AwsLoginEnded:
    """The AWS CLI, its SSO login over."""

    def __call__(self, argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, "", "Error when retrieving token from sso: Token has expired")


def _stops(rt, upstream, monkeypatch) -> None:
    """Six requests the gateway stops, each for a reason of its own wording: a body it cannot read, a body it does not
    read on a route that sends none it does not read, a masking that failed, an AWS login that ended, a masking that
    failed while the request was checked, and a body that would decode past a server's bound."""
    rt.gateway.upstreams += [
        Upstream("gemini", "/gemini", upstream.url, refuses_unread=True),
        BedrockUpstream(runtime=upstream.url, control=upstream.url),
    ]
    rt.gateway.aws = AwsCredentialStore(which=lambda name: "aws", run=_AwsLoginEnded())
    bedrock = f"/bedrock/us-east-1/profile/work/{profile_tag(rt.gateway.bridge_token, 'work')}"
    bedrock += "/model/us.anthropic.claude-opus-5-5/invoke"
    call = b'{"anthropic_version":"bedrock-2023-05-31","max_tokens":5,"messages":[{"role":"user","content":"hello"}]}'
    json_type = {"content-type": "application/json"}
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        unreadable = client.post(
            "/openai/v1/chat/completions", content=b'{"model": "gpt-4.1-mini", "messages": [', headers=AUTH
        )
        assert unreadable.status_code == 400
        assert client.post("/gemini/v1beta/files", json={"file": {"display_name": "notes"}}).status_code == 400

        def vault_full(*args, **kwargs):
            raise MaskingError("the vault is full")

        with monkeypatch.context() as patched:
            patched.setattr(rt.mediator, "mask_value", vault_full)
            body = {"model": GPT, "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
            assert client.post("/openai/v1/chat/completions", json=body, headers=AUTH).status_code == 400
        assert client.post(bedrock, content=call, headers=CLAUDE | json_type).status_code == 400

        def lacking(self, *_):
            raise MaskingError("a value this vault's store keeps is not in memory")

        with monkeypatch.context() as patched:  # the check's masker: a masking that fails outside the masking's try
            patched.setattr(Masker, "_matched", lacking)
            # A text the conversation has not checked yet (Bedrock's call above said "hello"): its check masks it.
            hello = {
                "model": CLAUDE_MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": "and now the invoice"}]
            }  # fmt: skip
            assert client.post("/anthropic/v1/messages", json=hello, headers=CLAUDE).status_code == 400
        rt.mediator.body_limit = 2000  # a server's max_request_mb, made small
        big = gzip.compress(
            json.dumps({"model": GPT, "messages": [{"role": "user", "content": "x" * 50_000}]}).encode()
        )
        packed = AUTH | json_type | {"content-encoding": "gzip"}
        assert client.post("/openai/v1/chat/completions", content=big, headers=packed).status_code == 413
    assert rt.recorder.drain()


@pytest.mark.parametrize("history", ["full", "off"])
def test_a_request_the_gateway_stops_says_why_with_the_code_of_its_reason(runtimes, upstream, monkeypatch, history):
    rt, edition = runtimes(history)
    _stops(rt, upstream, monkeypatch)
    assert [(e["type"], e["reason"]) for e in edition.events] == [
        ("stopped", "unreadable"), ("stopped", "unread-request"), ("stopped", "masking-failed"),
        ("stopped", "aws-signing"), ("stopped", "masking-failed"), ("stopped", "too-large"),
    ]  # fmt: skip
    assert all(e["masked"] == {} and e["stopped"] == 1 for e in edition.events)


@pytest.mark.parametrize("history", ["full", "off"])
def test_a_stop_recorded_with_an_error_is_stopped_for_one_reason_whatever_the_history(runtimes, monkeypatch, history):
    """The security re-review's N2: the error an exchange is recorded with (a request the transport refused after an
    attempt keeps no refusal code of its own: ``coverage-limit``) is read for the stop's reason (``unread-request``,
    3505675c). The event made beside the history's record carried no error, so with the history on the same stop was
    counted for another reason than with it off. The same stopped request (a leak, recorded with that error) now gives
    the same reason either way."""
    from latet.gateway import forward

    rt, edition = runtimes(history)
    recorded = forward.Forwarder._record

    def with_the_transports_error(self, outcome, status=None, error=None):
        recorded(self, outcome, status, error or "coverage-limit")

    monkeypatch.setattr(forward.Forwarder, "_record", with_the_transports_error)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        stop = {"model": CLAUDE_MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": f"key {GH_PAT}"}]}
        assert client.post("/anthropic/v1/messages", json=stop, headers=CLAUDE).status_code == 400
    assert rt.recorder.drain()
    assert [(e["type"], e["reason"]) for e in edition.events] == [("stopped", "unread-request")]
