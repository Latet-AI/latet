"""Production data: values by the field they sit in (secret columns, field rules, strict data mode)."""

import contextlib
import datetime
import hashlib
import itertools
import json
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import re2
import yaml

from latet.config import Config, FieldRule, IdentityConfig, ProjectEntry, ProjectSettings, load_config, save_config
from latet.engine import pipeline as pipeline_module
from latet.engine.build import build
from latet.engine.detectors import fields
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.fields import FieldDetector, cells
from latet.engine.masker import MaskReport
from latet.engine.merge import merge_spans
from latet.engine.pipeline import WITHHELD_DUMP
from latet.engine.rehydrate import restore_text
from latet.engine.surrogates.generators import Drbg, email_domain, gen_data
from latet.engine.types import EntityClass, SlotKind, Span
from latet.engine.vault import Entity, Vault
from latet.gateway import mediate as mediate_module
from latet.gateway.mediate import REWRITE, Mediator
from latet.gateway.routes import Upstream
from latet.projects import effective_config
from latet.protocols import anthropic, openai_chat, openai_responses
from latet.protocols.base import map_json_values
from latet.runtime import make_runtime
from latet.storage.db import Store

from ..conftest import ROOT_KEY

CASES = Path(__file__).parents[1] / "data" / "llm_review" / "cases.yaml"

PSQL = """ id | stato     | importo | nota
----+-----------+---------+---------------------------------
 17 | SPEDITO   |  1250.5 | richiamare al +39 347 555 0192
 18 | ANNULLATO |      99 | cliente scontento
(2 rows)
"""


def _mediator(**config) -> tuple[Mediator, object]:
    built = build(Config(**config), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    mediator.dump_guard = config.get("dump_guard")
    return mediator, built.engine.session("s")


def _mask(mediator, session, text, tool="Bash", kind=SlotKind.TOOL_RESULT):
    return mediator.mask_value(text, kind, session, MaskReport(), tool=tool)


def _left(mediator, session, text, kind=SlotKind.TOOL_RESULT):
    """The request that carried ``text`` left in the conversation ``session`` (a key): the final check let it out,
    and keeps it as left. A text sent again leaves as it left only once it is known to have left so."""
    assert mediator.verifier.verify([(text, kind, False)], session).blocking == []


def _by_field(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for cell in cells(text):
        out.setdefault(cell.field, []).append(text[cell.start : cell.end])
    return out


# --- one parser per format -------------------------------------------------------------------------


def test_psql_and_markdown_tables():
    assert _by_field(PSQL) == {
        "id": ["17", "18"],
        "stato": ["SPEDITO", "ANNULLATO"],
        "importo": ["1250.5", "99"],
        "nota": ["richiamare al +39 347 555 0192", "cliente scontento"],
    }
    markdown = "| name | city |\n|------|:----:|\n| Marta Bianchi | Brescia |\n| Ugo Neri | Lecco |\n"
    assert _by_field(markdown) == {"name": ["Marta Bianchi", "Ugo Neri"], "city": ["Brescia", "Lecco"]}


def test_csv_tsv_and_semicolon_tables():
    csv = 'id,name,note\n1,"Rossi, Mario","ok"\n2,Ugo,\n'
    assert _by_field(csv) == {"id": ["1", "2"], "name": ["Rossi, Mario", "Ugo"], "note": ["ok"]}
    assert _by_field("id\tcode\n1\tAB-12\n") == {"id": ["1"], "code": ["AB-12"]}
    assert _by_field("First Name;Last Name\nMarta;Bianchi\nUgo;Neri\n") == {
        "First Name": ["Marta", "Ugo"],
        "Last Name": ["Bianchi", "Neri"],
    }


def test_prose_with_commas_is_no_table():
    prose = "To pass exception information, use the keyword argument exc_info with\na true value, e.g.\n"
    assert cells(prose) == []
    # Names of several words need two rows under them: one line after such a "header" is prose.
    assert cells("Dear Mario, thanks for the call\nsee you soon, bye\n") == []


def test_html_tables_and_browser_snapshots():
    html = "<table><tr><th>Name</th><th>Email</th></tr><tr><td> Marta </td><td>m@x.it</td></tr></table>"
    assert _by_field(html) == {"Name": ["Marta"], "Email": ["m@x.it"]}
    snapshot = """- table "Orders":
  - row:
    - columnheader "Customer"
    - columnheader "State"
  - row:
    - cell "Marta Bianchi"
    - cell "SHIPPED"
"""
    assert _by_field(snapshot) == {"Customer": ["Marta Bianchi"], "State": ["SHIPPED"]}


def test_key_values_and_json_fragments():
    log = '10:00 INFO order=88123 user=mrossi note="call back"\nsingle=1 only\n'
    assert _by_field(log) == {"order": ["88123"], "user": ["mrossi"], "note": ["call back"]}
    assert _by_field('body={"destinatario":"Franca Bellini","valore":4200}') == {
        "destinatario": ["Franca Bellini"],
        "valore": ["4200"],
    }


def test_json_values_with_their_paths():
    seen = []

    def note(value, path, number):
        seen.append((path, value, number))
        return value

    doc = json.dumps({"items": [{"sku": "AB-1", "qty": 3, "ok": True}], "next": None})
    assert map_json_values(doc, note, numbers=True) == doc  # nothing changed: the same bytes
    assert seen == [("items[].sku", "AB-1", False), ("items[].qty", "3", True)]
    out = map_json_values(doc, lambda v, p, n: "42" if n else v, numbers=True)
    assert json.loads(out)["items"][0]["qty"] == 42  # a number stays a number


# --- strict data mode ------------------------------------------------------------------------------


def test_strict_masks_every_value_and_keeps_the_names():
    mediator, session = _mediator(data="strict")
    out = _mask(mediator, session, PSQL)
    for value in ("SPEDITO", "ANNULLATO", "1250.5", "cliente scontento", "347 555 0192"):
        assert value not in out.text
    assert out.text.splitlines()[0] == PSQL.splitlines()[0]  # the header row is untouched
    assert " 99 " in out.text and " 17 " in out.text  # one or two characters stay in clear
    classes = {r.original: (r.etype, r.eclass) for r in out.replacements}
    assert classes["SPEDITO"] == ("DATA", EntityClass.STRUCT)
    # A finding inside a value keeps its own type: the phone number in the note is a phone number.
    assert classes["+39 347 555 0192"][0] == "PHONE"
    assert classes["richiamare al"] == ("DATA", EntityClass.STRUCT)


@pytest.mark.parametrize(
    ("settings", "mode"),
    [
        ({}, "normal"),
        ({"profile": "max"}, "strict"),  # on by default in max
        ({"profile": "secrets"}, "normal"),
        ({"profile": "max", "data": "normal"}, "normal"),  # what you set wins, both ways
        ({"data": "strict"}, "strict"),
        ({"profile": "secrets", "data": "strict"}, "strict"),
    ],
)
def test_strict_data_mode_is_the_profiles_unless_you_set_it(settings, mode):
    assert Config(**settings).data_mode() == mode
    mediator, session = _mediator(**settings)
    assert ("SPEDITO" in _mask(mediator, session, PSQL).text) is (mode == "normal")


def test_left_to_the_profile_it_is_not_written_down(tmp_path):
    path = save_config(Config(profile="max"), tmp_path / "config.yaml")
    assert "data:" not in path.read_text("utf-8")  # another profile, later, still decides
    assert load_config(path).data_mode() == "strict"
    assert load_config(save_config(Config(profile="max", data="normal"), tmp_path / "set.yaml")).data == "normal"


def test_strict_leaves_files_alone_and_can_cover_some_tools_only():
    mediator, session = _mediator(data="strict")
    assert "SPEDITO" in _mask(mediator, session, PSQL, tool="Read").text
    mediator, session = _mediator(data="strict", data_tools=["mcp__postgres__*"])
    assert "SPEDITO" in _mask(mediator, session, PSQL, tool="Bash").text
    assert "SPEDITO" not in _mask(mediator, session, PSQL, tool="mcp__postgres__query").text
    mediator, session = _mediator()  # normal mode: a table's values are left to the usual detectors
    assert "SPEDITO" in _mask(mediator, session, PSQL).text


def test_strict_leaves_web_results_alone_unless_named():
    results = (
        'Web search results for query: "carbonara"\n\nLinks: '
        '[{"title":"Carbonara: the history","url":"https://example.org/carbonara"}]'
    )
    mediator, session = _mediator(data="strict")
    for tool in ("WebSearch", "WebFetch"):
        assert _mask(mediator, session, results, tool=tool).text == results
    assert "Carbonara: the history" not in _mask(mediator, session, results, tool="mcp__search__query").text
    mediator, session = _mediator(data="strict", data_tools=["WebSearch"])  # named: covered
    assert "Carbonara: the history" not in _mask(mediator, session, results, tool="WebSearch").text


def test_a_value_of_a_structure_never_becomes_a_known_value():
    mediator, session = _mediator(data="strict")
    _mask(mediator, session, PSQL)
    code = 'if order.stato == "SPEDITO":\n    notify("cliente scontento")\n'
    assert _mask(mediator, session, code, tool="Read").text == code
    # Nor another occurrence in the same output, out of the table.
    out = _mask(mediator, session, PSQL + "SPEDITO orders: 1\n")
    assert out.text.endswith("SPEDITO orders: 1\n")


def test_a_key_or_a_term_in_a_value_keeps_its_type_next_to_an_escape():
    """Read again with its escapes blanked out, a value of a structure covers what was found in it: the key and the
    term stay what they are, a secret and a term, not just data."""
    mediator, session = _mediator(data="strict", terms=[{"value": "tesla", "type": "CLIENT"}])
    config_map = json.dumps({"data": {"app.env": "A=1\nADAFRUIT=0123456789abcdef0123456789abcdef\nB=2"}})
    listing = "Name,Path\nspec.pdf,D:\\clients\\tesla\\spec.pdf\n"  # `\t`: a folder, not an escape
    for text, eclass in ((config_map, EntityClass.SECRET), (listing, EntityClass.CUSTOM)):
        out = _mask(mediator, session, text)
        assert eclass in {r.eclass for r in out.replacements}, text


def test_an_escape_in_a_value_of_a_structure_stays_as_written():
    mediator, session = _mediator(data="strict")
    body = json.dumps({"to": "Anna\nluca.neri@acme.it", "note": "line one\nline two"})
    out = _mask(mediator, session, "HTTP/1.1 200 OK\n\n" + body)  # JSON in a command's output: read as text
    masked = json.loads(out.text.split("\n\n", 1)[1])  # still JSON
    assert masked["to"].count("\n") == 1 and masked["note"].count("\n") == 1  # the line breaks where they were
    restored, _ = restore_text(out.text, mediator.engine.restores(session, json_mode=False))
    assert json.loads(restored.split("\n\n", 1)[1]) == json.loads(body)


def test_hundreds_of_short_numbers_are_masked_however_few_fakes_their_shape_has():
    """A query's rows with ids of three digits: there are 900 such numbers, fewer than the ids and their fakes. A fake
    grows a digit once its shape has none left, and one that shows up only inside a longer number is no clash."""
    mediator, session = _mediator(data="strict")
    rows = json.dumps([{"id": i, "ts": 1791590000 + i} for i in range(100, 600)])
    out = _mask(mediator, session, rows)
    ids = [row["id"] for row in json.loads(out.text)]
    assert all(fake != real for fake, real in zip(ids, range(100, 600), strict=True))
    assert sum(len(str(fake)) == 3 for fake in ids) >= 300  # most keep their shape
    restored, _ = restore_text(out.text, mediator.engine.restores(session, json_mode=False))
    assert json.loads(restored) == json.loads(rows)


def test_a_query_written_with_the_fakes_runs_with_the_real_values():
    mediator, session = _mediator(data="strict")
    out = _mask(mediator, session, PSQL)
    fake = next(r.replacement for r in out.replacements if r.original == "SPEDITO")
    restores = mediator.engine.restores(session, json_mode=False)
    query, _ = restore_text(f"SELECT * FROM ordini WHERE stato = '{fake}'", restores)
    assert query == "SELECT * FROM ordini WHERE stato = 'SPEDITO'"
    # Only as a whole word: the same letters inside another word stay.
    assert restore_text(f"x{fake}", restores)[0] == f"x{fake}"


def test_json_outputs_by_path_and_the_wrappers_of_agents():
    mediator, session = _mediator(data="strict", fields=[FieldRule(name="items[].sku", mask=False)])
    rest = json.dumps({"items": [{"sku": "PRD-2207", "supplier": "Hydra Componenti", "cost": 41.7, "qty": 3}]})
    out = json.loads(_mask(mediator, session, rest).text)
    item = out["items"][0]
    assert item["sku"] == "PRD-2207" and item["qty"] == 3
    assert item["supplier"] != "Hydra Componenti"
    assert isinstance(item["cost"], float) and item["cost"] != 41.7
    # Codex wraps a command's output: the text inside is read as text, the metadata is left alone.
    codex = json.dumps({"output": PSQL, "metadata": {"exit_code": 0, "duration_seconds": 0.512}})
    out = json.loads(_mask(mediator, session, codex, tool="shell").text)
    assert "SPEDITO" not in out["output"] and out["metadata"] == {"exit_code": 0, "duration_seconds": 0.512}
    # MCP: the content blocks keep their type; JSON in a text block is read with its paths.
    mcp = json.dumps({"content": [{"type": "text", "text": json.dumps([{"id": 1001, "stato": "MOROSO"}])}]})
    out = json.loads(_mask(mediator, session, mcp, tool="mcp__db__query").text)
    assert out["content"][0]["type"] == "text"
    rows = json.loads(out["content"][0]["text"])
    assert rows[0]["stato"] != "MOROSO" and rows[0]["id"] != 1001


@pytest.mark.parametrize(
    ("fields", "site", "contact"),
    [
        ([], "https://portal.acme-intra.it/home", "ops@acme-intra.it"),
        ([FieldRule(name="contact", type="EMAIL")], "https://portal.acme-intra.local/home", "anna@acme-intra.local"),
    ],
)
def test_an_address_brings_its_domain_to_the_keys_before_it(fields, site, contact):
    """A JSON output is masked value by value, in the order of its keys, and the vault learns a company's domain with
    its address: the domain bare in a key before the address's is masked all the same, with the domain of the address's
    own fake, so the daemon's check stops nothing, nor when the output is sent again."""
    mediator, session = _mediator(fields=fields)
    output = json.dumps({"site": site, "contact": contact})
    out = _mask(mediator, session, output)
    fake = mediator.engine.vault.by_original(contact).surrogate
    domain, fake_domain = contact.rpartition("@")[2], fake.rpartition("@")[2]
    assert json.loads(out.text) == {"site": site.replace(domain, fake_domain), "contact": fake}
    assert [r.original for r in out.replacements] == [domain, contact]
    assert mediator.verifier.verify([(out.text, SlotKind.TOOL_RESULT, False)], "gw|s").blocking == []
    assert _mask(mediator, session, output).text == out.text  # sent again: masked the same way


def test_a_secret_found_by_its_key_is_masked_in_the_keys_before_it():
    """The value of a `password` key is a secret, and so is the same password in a note before the key: it gets the
    same fake there, as in every later output, instead of leaving in clear."""
    mediator, session = _mediator()
    output = json.dumps({"note": "log in with Tr0ub4dor-Horse-9", "password": "Tr0ub4dor-Horse-9"})
    masked = json.loads(_mask(mediator, session, output).text)
    assert masked["password"] != "Tr0ub4dor-Horse-9" and masked["note"] == f"log in with {masked['password']}"


def test_an_output_sent_before_its_domain_was_known_is_sent_again_as_it_was():
    """An output that named the domain before any address did left with it in clear; once an address in another output
    teaches the vault the domain, the first output is still sent again exactly as it left."""
    mediator, session = _mediator()
    before = json.dumps({"site": "https://portal.acme-intra.it/home"})
    assert _mask(mediator, session, before).text == before
    _left(mediator, session.key, before)
    _mask(mediator, session, json.dumps({"site": "https://portal.acme-intra.it/home", "contact": "ops@acme-intra.it"}))
    assert _mask(mediator, session, before).text == before


PORTAL = "https://portal.acme-intra.it/home"


def _blocking(mediator, text, session="s") -> list[str]:
    """What the daemon's check stops in a request of the conversation ``session`` that carries ``text`` as an output."""
    sent = [(text, SlotKind.TOOL_RESULT, False)]
    return [leak.preview for leak in mediator.verifier.verify(sent, f"gw|{session}").blocking]


def test_a_value_left_in_clear_before_the_vault_knew_it_is_masked_in_the_next_output_that_holds_it():
    """An output named a domain before any address taught it to the vault, and left with it in clear. A new output with
    the same value, once an address taught it, has it masked, though that value was masked before and kept: the daemon's
    check stops nothing, and the first output is still sent again as it left."""
    mediator, session = _mediator()
    first = json.dumps({"site": PORTAL})
    assert _mask(mediator, session, first).text == first
    _left(mediator, session.key, first)
    _mask(mediator, session, "write to ops@acme-intra.it")
    fake = mediator.engine.vault.by_original("acme-intra.it").surrogate
    for output in ({"site": PORTAL, "status": "down"}, {"site": PORTAL, "contact": "ops@acme-intra.it"}):
        out = _mask(mediator, session, json.dumps(output))
        assert json.loads(out.text)["site"] == PORTAL.replace("acme-intra.it", fake)
        assert _blocking(mediator, out.text) == []
    assert _mask(mediator, session, first).text == first


@pytest.mark.parametrize("text", [json.dumps({"site": PORTAL}), f"the portal is at {PORTAL}"])
def test_a_text_left_in_clear_before_the_vault_knew_a_value_in_it_is_masked_in_another_conversation(text):
    """The same output, or the same text, in another conversation is masked with what the vault knows now; the
    conversation it left in clear sends it again as it left."""
    mediator, session = _mediator()
    assert _mask(mediator, session, text).text == text
    _left(mediator, session.key, text)
    _mask(mediator, session, "write to ops@acme-intra.it")
    other = _mask(mediator, mediator.engine.session("other"), text)
    assert "acme-intra.it" not in other.text and _blocking(mediator, other.text, "other") == []
    assert _mask(mediator, session, text).text == text


def test_a_value_masked_before_gets_the_domain_a_later_key_of_a_new_output_teaches():
    """A value left in clear by an earlier output, in a new output whose later key holds an address at its domain: the
    address teaches the vault the domain, and the value is masked in this output as one masked now is."""
    mediator, session = _mediator()
    first = json.dumps({"site": PORTAL})
    assert _mask(mediator, session, first).text == first
    _left(mediator, session.key, first)
    out = _mask(mediator, session, json.dumps({"site": PORTAL, "contact": "ops@acme-intra.it"}))
    assert "acme-intra.it" not in out.text and _blocking(mediator, out.text) == []
    assert _mask(mediator, session, first).text == first


def test_in_claude_code_another_conversation_sends_masked_what_one_sent_in_clear_before():
    """Claude Code's plugin masks an output that left in clear before the vault knew its domain: in another conversation
    the same output is masked, and the daemon's check of that conversation's request stops nothing."""
    from latet.bridge.api import Bridge

    mediator, _ = _mediator()
    bridge = Bridge(mediator, "token")

    def mask(session: str, text: str) -> str:
        _, out, _, _, _ = bridge._mask({"session": session, "items": [{"text": text, "kind": "tool_result"}]})
        return out[0]["text"]

    output = json.dumps({"site": PORTAL})
    assert mask("a", output) == output
    _left(mediator, "cc:a", output)  # the plugin's check let the request out
    mask("a", "write to ops@acme-intra.it")
    other = mask("b", output)
    assert "acme-intra.it" not in other and _blocking(mediator, other, "cc:b") == []
    assert mask("a", output) == output


def _conversation(prompt: str, *outputs: str) -> bytes:
    """A request to Anthropic's API: ``prompt``, then a command and its output for each of ``outputs``."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    for n, output in enumerate(outputs):
        call = {"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": "curl"}}
        result = {"type": "tool_result", "tool_use_id": f"t{n}", "content": output}
        messages.append({"role": "assistant", "content": [call]})
        messages.append({"role": "user", "content": [result]})
    return json.dumps({"model": "x", "messages": messages}).encode()


def test_through_the_gateway_another_conversation_sends_masked_what_one_sent_in_clear_before():
    """An agent through the gateway, where no final check stops anything: an output with a domain the vault did not know
    left in clear. Its conversation sends it again as it left, and another conversation sends it masked."""
    mediator, _ = _mediator()
    upstream = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
    headers = {"x-api-key": "test", "content-type": "application/json"}

    def outputs(raw: bytes) -> list[str]:
        prepared = mediator.prepare(upstream, "POST", "/anthropic/v1/messages", headers, raw)
        assert prepared.mode == REWRITE and prepared.session is not None
        sent = [message["content"][0]["content"] for message in json.loads(prepared.body)["messages"][2::2]]
        for text in sent:  # the request leaves
            _left(mediator, prepared.session.key, text)
        return sent

    site = json.dumps({"site": PORTAL})
    assert outputs(_conversation("is the portal up?", site)) == [site]
    first = outputs(_conversation("is the portal up?", site, "write to ops@acme-intra.it"))
    assert first[0] == site and "acme-intra.it" not in first[1]
    [other] = outputs(_conversation("check the portal", site))
    assert "acme-intra.it" not in other


def test_through_the_gateway_an_output_gets_the_domain_a_later_output_of_the_same_request_teaches():
    """One request brings two outputs new to the conversation: the first names a domain, the second holds an address at
    it, which teaches the vault the domain. The first is masked too, wherever it stands in the request, the daemon's
    check stops nothing, and the conversation sends both again as they left."""
    mediator, _ = _mediator()
    upstream = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
    headers = {"x-api-key": "test", "content-type": "application/json"}

    def outputs(raw: bytes) -> list[str]:
        prepared = mediator.prepare(upstream, "POST", "/anthropic/v1/messages", headers, raw)
        assert prepared.mode == REWRITE
        return [message["content"][0]["content"] for message in json.loads(prepared.body)["messages"][2::2]]

    site, contact = json.dumps({"site": PORTAL}), json.dumps({"contact": "ops@acme-intra.it"})
    sent = outputs(_conversation("is the portal up?", site, contact, site))
    fake = mediator.engine.vault.by_original("acme-intra.it").surrogate
    assert json.loads(sent[0])["site"] == PORTAL.replace("acme-intra.it", fake) and sent[2] == sent[0]
    assert [_blocking(mediator, text) for text in sent] == [[], [], []]
    assert outputs(_conversation("is the portal up?", site, contact, site, "done")) == [*sent, "done"]


def test_in_claude_code_an_output_gets_the_domain_a_later_output_of_the_same_call_teaches():
    """Claude Code's plugin sends two outputs to mask in one call, the second teaching the vault the domain the first
    names: the first comes back masked, and the daemon's check of the request stops nothing."""
    from latet.bridge.api import Bridge

    mediator, _ = _mediator()
    bridge = Bridge(mediator, "token")
    items = [json.dumps({"site": PORTAL}), "write to ops@acme-intra.it"]
    _, out, _, _, _ = bridge._mask({"session": "a", "items": [{"text": t, "kind": "tool_result"} for t in items]})
    assert "acme-intra.it" not in out[0]["text"] and out[0]["count"] == 1
    assert [_blocking(mediator, entry["text"], "cc:a") for entry in out] == [[], []]


def test_an_output_sent_before_is_sent_as_it_left_when_a_new_output_of_the_request_teaches_its_domain():
    """The output that left in clear is not masked by the second pass over the request's texts: only the texts new to
    the conversation are."""
    mediator, _ = _mediator()
    upstream = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
    headers = {"x-api-key": "test", "content-type": "application/json"}

    def outputs(raw: bytes) -> list[str]:
        prepared = mediator.prepare(upstream, "POST", "/anthropic/v1/messages", headers, raw)
        assert prepared.session is not None
        sent = [message["content"][0]["content"] for message in json.loads(prepared.body)["messages"][2::2]]
        for text in sent:  # the request leaves
            _left(mediator, prepared.session.key, text)
        return sent

    site, status = json.dumps({"site": PORTAL}), json.dumps({"url": PORTAL, "status": "down"})
    assert outputs(_conversation("is the portal up?", site)) == [site]
    sent = outputs(_conversation("is the portal up?", site, status, "write to ops@acme-intra.it"))
    assert sent[0] == site and "acme-intra.it" not in sent[1] + sent[2]


def test_numbers_and_dates_keep_their_shape():
    rng = Drbg(ROOT_KEY, b"seed")
    for original in ("2024-03-14", "2024-03-14T10:22:33Z", "2024-03-14 10:22:33.123+02:00"):
        fake = gen_data(original, rng)
        assert len(fake) == len(original) and fake != original
        datetime.datetime.fromisoformat(fake.replace("Z", "+00:00"))  # still a valid date and time
    for original in ("1250.5", "52000", "-12.25", "100"):
        fake = gen_data(original, rng)
        assert len(fake) == len(original) and fake.lstrip("-")[0] != "0"


def test_a_web_address_keeps_its_scheme_and_nothing_else():
    """The model still reads a link (https://), and the scheme says nothing of yours: the host, the path, a token in the
    query and the credentials of a connection string are masked."""
    rng = Drbg(ROOT_KEY, b"seed")
    for original, scheme, secret in (
        ("https://brigbnb.example/verify?token=8f3a9c", "https://", "brigbnb"),
        ("postgres://ann:s3cret@db.acme.lan/app", "postgres://", "s3cret"),
    ):
        fake = gen_data(original, rng)
        assert fake.startswith(scheme) and secret not in fake and fake != original


@pytest.mark.parametrize("original", ["https://", "file:///", "s3://", "acmebank://pay/42"])
def test_a_scheme_is_kept_only_when_common_and_followed_by_a_value(original):
    """A scheme alone (what is left of a link whose host or user is masked apart) has a fake of its own, never itself;
    an app's own scheme (acmebank://) can name a company, and is masked whole."""
    fake = gen_data(original, Drbg(ROOT_KEY, b"seed"))
    assert fake != original
    assert not fake.startswith("acmebank")


@pytest.mark.parametrize(
    "line",
    [
        "url=https://marta.galli@gmail.com user=ann",
        "url=https:// user=ann",
        "url=s3:// user=ann",
    ],
)
def test_strict_mode_masks_a_link_whose_value_is_masked_apart(line):
    """A link whose user or host is found apart leaves its scheme alone in strict mode's value: it is masked, and the
    request is not stopped (it stayed in the conversation, and stopped every later one)."""
    mediator, session = _mediator(data="strict")
    out = _mask(mediator, session, line)
    assert "marta.galli" not in out.text and "ann" not in out.text.split("user=")[1]


# --- secret columns, field rules, unlocks ----------------------------------------------------------


def test_secret_columns_are_masked_even_with_the_name_in_the_header():
    mediator, session = _mediator()
    csv = "id,email,api_key,password_hash,reset_token,role\n7,ops@acme-intra.it,ak_live_9fK2LmQx7R,$2b$12$Kq3v0rZQ8x,8f14e45fceea167a,admin\n"
    out = _mask(mediator, session, csv)
    for value in ("ops@acme-intra.it", "ak_live_9fK2LmQx7R", "$2b$12$Kq3v0rZQ8x", "8f14e45fceea167a"):
        assert value not in out.text
    assert out.text.startswith("id,email,api_key,password_hash,reset_token,role\n") and out.text.endswith(",admin\n")
    assert {r.rule for r in out.replacements if r.eclass == EntityClass.SECRET} == {
        "field-secret:api_key",
        "field-secret:password_hash",
        "field-secret:reset_token",
    }
    # A JSON key says it as well.
    out = _mask(mediator, session, json.dumps({"user": {"name": "x", "password": "hunter22"}}))
    assert "hunter22" not in out.text


def test_a_connection_string_column_keeps_all_but_its_credentials():
    mediator, session = _mediator()
    row = {"connection_string": "Server=db;User Id=app;Password=Sup3rS3cret!;", "env": "prod"}
    out = _mask(mediator, session, json.dumps(row))
    assert "Sup3rS3cret!" not in out.text and "Server=db;User Id=app;Password=" in out.text
    placeholders = json.dumps({"connection_string": "Server=db;User Id={{user}};Password={{password}};"})
    assert _mask(mediator, session, placeholders).text == placeholders
    # In strict data mode the rest of the string is data, and the password stays a secret.
    mediator, session = _mediator(data="strict")
    out = _mask(mediator, session, "name,connection_string\nmain,Server=db;Password=Sup3rS3cret!;\n")
    assert "Sup3rS3cret!" not in out.text and "Server=db" not in out.text
    kinds = {(r.etype, r.eclass) for r in out.replacements}
    assert kinds >= {("PASSWORD", EntityClass.SECRET), ("DATA", EntityClass.STRUCT)}


def test_field_rules_mask_a_column_as_your_type_in_its_structure_only():
    mediator, session = _mediator(fields=[FieldRule(name="note_*", type="note")])
    table = "id,note_interne\n1,cliente VIP da trattare bene\n"
    out = _mask(mediator, session, table)
    assert "cliente VIP" not in out.text
    assert [(r.etype, r.eclass, r.rule) for r in out.replacements] == [("NOTE", EntityClass.STRUCT, "field:note_*")]
    assert _mask(mediator, session, "cliente VIP da trattare bene", tool="Read").text == "cliente VIP da trattare bene"


@pytest.mark.parametrize("address", ["anna@acme-intra.local", "daniel dot okafor at stmarys-health dot org"])
def test_an_address_in_a_field_typed_email_brings_its_domain_along(address):
    """A field rule typed EMAIL masks a column's addresses in their structure, and no detector finds these by
    themselves (a TLD no pattern knows, an address written out in words): the domain bare in another column is masked
    all the same, with the domain of the address's own fake, so the daemon's check of the output stops nothing."""
    mediator, session = _mediator(fields=[FieldRule(name="contact", type="EMAIL")])
    out = _mask(mediator, session, f"id,contact,site\n1,{address},https://portal.{email_domain(address)}/login\n")
    fake = mediator.engine.vault.by_original(address).surrogate
    assert out.text == f"id,contact,site\n1,{fake},https://portal.{email_domain(fake)}/login\n"
    assert mediator.verifier.verify([(out.text, SlotKind.TOOL_RESULT, False)], "gw|s").blocking == []


def test_an_address_in_a_field_typed_email_stays_whole_in_a_later_output():
    """From the first output on, its domain is a value in the vault, found again in the address itself: the address is
    not cut around it, and keeps its fake."""
    mediator, session = _mediator(fields=[FieldRule(name="contact", type="EMAIL")])
    _mask(mediator, session, "id,contact\n1,anna@acme-intra.local\n")
    fake = mediator.engine.vault.by_original("anna@acme-intra.local").surrogate
    assert _mask(mediator, session, "id,contact\n2,anna@acme-intra.local\n").text == f"id,contact\n2,{fake}\n"


def test_an_address_in_a_field_typed_email_brings_a_companys_domain_only():
    """A mail provider's domain stays in clear, as beside any address; and the address is the field's value, not the
    same words out of the table."""
    mediator, session = _mediator(fields=[FieldRule(name="contact", type="EMAIL")])
    table = "id,contact,note\n1,jane dot doe at gmail dot com,not on gmail.com\n"
    assert _mask(mediator, session, table).text.endswith(",not on gmail.com\n")
    address = "daniel dot okafor at stmarys-health dot org"
    out = _mask(mediator, session, f"id,contact\n1,{address}\n\nwrite to {address}\n")
    assert out.text.endswith(f"\nwrite to {address}\n")


def test_an_unlocked_field_stays_in_clear_but_keeps_its_secrets():
    rules = [FieldRule(name="stato", mask=False), FieldRule(name="*", mask=False)]
    detector = FieldDetector(rules[:1], strict=True)
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = {PSQL[s.start : s.end] for s in detector.detect(PSQL, ctx)}
    assert "SPEDITO" not in found and "cliente scontento" in found
    everything = FieldDetector(rules, strict=True)
    csv = "user,api_key\nmario,ak_live_9fK2LmQx7R\n"
    assert [(csv[s.start : s.end], s.eclass) for s in everything.detect(csv, ctx)] == [
        ("ak_live_9fK2LmQx7R", EntityClass.SECRET)
    ]


@pytest.mark.parametrize(
    "placeholder",
    ["${from}", "${user.email}", "{{ subject }}", "$MAIL_FROM", "%APPDATA%", "%(name)s", "{recipient}"],
)
def test_strict_mode_leaves_a_placeholder_that_names_a_value_rather_than_holding_one(placeholder):
    """Code a command prints (grep, cat) holds `from="${from}"`: the value is a variable's name, nothing of yours. The
    real value beside it is still masked."""
    line = f'console.log(`Sending from="{placeholder}" to="ann.ross@acme.com" code="X4-77"`);'
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = {line[s.start : s.end] for s in FieldDetector(strict=True).detect(line, ctx)}
    assert placeholder not in found
    assert {"ann.ross@acme.com", "X4-77"} <= found


@pytest.mark.parametrize("value", ["$Secr3tPass", "{marta.galli}"])
def test_strict_mode_masks_a_value_that_only_looks_like_a_placeholder(value):
    """A shell's variable is named in capitals ($MAIL_FROM), a format's field has no dots ({recipient}): a value of
    yours that begins with a dollar or sits in braces is masked."""
    line = json.dumps({"level": "info", "note": value, "guest": "ann"})
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = {line[s.start : s.end] for s in FieldDetector(strict=True).detect(line, ctx)}
    assert value in found, found


def test_strict_mode_leaves_the_style_and_class_of_markup_a_command_prints():
    """An email template a command prints (grep, cat) holds style="display: inline-block; ..." and class="btn": how the
    page looks, nothing of yours. A data attribute beside them is still masked."""
    line = (
        '<a class="btn btn-primary" style="display: inline-block; background: #2563eb; color: white; '
        'padding: 12px 24px;" data-guest="Ann Ross">Verify</a>'
    )
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = {line[s.start : s.end] for s in FieldDetector(strict=True).detect(line, ctx)}
    assert not any("inline-block" in value or "btn-primary" in value for value in found), found
    assert "Ann Ross" in found


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("name,class\nMarta Galli,3B Liceo Volta\n", "3B Liceo Volta"),
        ('{"class": "ASA-III", "name": "Ann Ross"}', "ASA-III"),
        ("level=info class=NYHA-III patient=ann", "NYHA-III"),
        ("if a < b then class=marta.galli user=ann", "marta.galli"),
        ('<p>ok</p> class="Liceo Volta" user="ann"', "Liceo Volta"),
        ('<div style="background: url(https://cdn.x/u/marta.galli.jpg)" id="hero">', "marta.galli"),
    ],
)
def test_strict_mode_masks_a_class_or_a_style_that_is_not_how_a_page_looks(text, value):
    """Only markup's own attributes say how a page looks: a column, a key or a log field named class holds a school's
    or a clinic's class, and a style that loads an address can name a person."""
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = [text[s.start : s.end] for s in FieldDetector(strict=True).detect(text, ctx)]
    assert any(value in v for v in found), found


def test_a_table_drawn_with_box_characters_is_read_as_a_table():
    """Node's console.table, psql's unicode style and the CLIs that draw with box characters: each value under its
    column, without the quotes console.table writes around a string."""
    node = (
        "┌─────────┬─────────────────────┬──────────────┐\n"
        "│ (index) │ ospite              │ check_in     │\n"
        "├─────────┼─────────────────────┼──────────────┤\n"
        "│ 0       │ 'Francesca Solieri' │ '2026-10-19' │\n"
        "│ 1       │ 'Thierry Noraz'     │ '2026-10-20' │\n"
        "└─────────┴─────────────────────┴──────────────┘\n"
    )
    by_field = _by_field(node)
    assert by_field["ospite"] == ["Francesca Solieri", "Thierry Noraz"]
    assert by_field["check_in"] == ["2026-10-19", "2026-10-20"]
    psql = " id │ name     │ email\n────┼──────────┼───────────────────\n  1 │ Ann Ross │ ann.ross@acme.com\n"
    assert _by_field(psql) == {"id": ["1"], "name": ["Ann Ross"], "email": ["ann.ross@acme.com"]}


def test_a_row_of_a_table_without_borders_keeps_its_columns_when_its_first_or_last_cell_is_empty():
    """psql draws no outer border by default: a NULL in the first or the last column is an empty cell there, not a
    border, and the row's values stay under their columns."""
    for bar, rule in (("│", "────┼─────────────┼───────┼──────────"), ("|", "----+-------------+-------+----------")):
        table = (
            f" id {bar} name        {bar} city  {bar} phone\n{rule}\n"
            f"    {bar} Marta Galli {bar} Turin {bar} 347 555 0192\n"
            f"  2 {bar} Bob Lee     {bar} Milan {bar} \n"
        )
        by_field = _by_field(table)
        assert by_field["name"] == ["Marta Galli", "Bob Lee"], bar
        assert by_field["city"] == ["Turin", "Milan"], bar
        assert by_field["phone"] == ["347 555 0192"], bar


def test_strict_mode_masks_a_row_of_a_table_that_does_not_fit_its_columns():
    """A bar inside a value gives a row one cell more than its header: it is masked whole, never left in clear."""
    table = " id │ name\n────┼──────────\n  1 │ Ann │ Ross\n"
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = [table[s.start : s.end] for s in FieldDetector(strict=True).detect(table, ctx)]
    assert any("Ann" in v and "Ross" in v for v in found), found
    # Not by a rule that keeps a column called row in clear: the row is no column.
    kept = FieldDetector(strict=True, rules=[FieldRule(name="row", mask=False)])
    assert any("Ann" in table[s.start : s.end] for s in kept.detect(table, ctx))


@pytest.mark.parametrize(
    "table",
    [
        ' id │ config\n────┼──────────────────────────────────────────\n  1 │ {"password": "Winter2024!", "user": "a"}\n',
        " id │ config\n────┼──────────────────────────────\n  1 │ password=Winter2024! user=ann\n",
        (
            "┌─────────┬──────────────────────────────┐\n"
            "│ (index) │ config                       │\n"
            "├─────────┼──────────────────────────────┤\n"
            '│ 0       │ \'{"apiKey":"Winter2024!xy"}\' │\n'
            "└─────────┴──────────────────────────────┘\n"
        ),
        ' id | config\n----+------------------------------------------\n  1 | {"password": "Winter2024!", "user": "a"}\n',
        '| id | config |\n|----|--------|\n| 1 | {"password": "Winter2024!"} |\n',
    ],
)
def test_a_secret_named_inside_a_cell_of_a_table_is_masked(table):
    """A cell can hold key=value pairs or JSON (psql's jsonb, a string console.table prints): a secret its name says is
    masked there as on any line, strict data mode or not."""
    ctx = DetectContext(SlotKind.TOOL_RESULT, tool="Bash")
    found = [table[s.start : s.end] for s in FieldDetector().detect(table, ctx)]
    assert any("Winter2024!" in v for v in found), found


def test_a_cut_value_keeps_every_character_masked():
    text = "note: call +39 347 555 0192 later"
    spans = [
        Span(6, 33, "DATA", EntityClass.STRUCT, "strict:note"),
        Span(11, 27, "PHONE", EntityClass.PII, "pii:phone"),
    ]
    merged = merge_spans(spans, text)
    assert [(text[s.start : s.end], s.etype) for s in merged] == [
        ("call", "DATA"),
        ("+39 347 555 0192", "PHONE"),
        ("later", "DATA"),
    ]


# --- the dump guard --------------------------------------------------------------------------------


def test_the_dump_guard_withholds_an_output_with_too_many_people():
    mediator, session = _mediator(dump_guard=5)
    many = "\n".join(f"cliente{i}@acme-mail.it" for i in range(8))
    out = _mask(mediator, session, many)
    assert out.withheld and out.text.startswith(WITHHELD_DUMP) and "acme-mail" not in out.text
    assert "8 personal values" in out.text and "the 5 allowed" in out.text
    few = "\n".join(f"cliente{i}@acme-mail.it" for i in range(5))
    assert not _mask(mediator, session, few).withheld
    assert not _mask(mediator, session, many, kind=SlotKind.USER_TEXT).withheld  # tool outputs only
    mediator.dump_guard = 0  # 0: no limit
    assert not _mask(mediator, session, many).withheld


def test_the_dump_guard_flags_the_exchange_once():
    mediator, _ = _mediator(dump_guard=3)
    rows = "\n".join(f"cliente{i}@acme-mail.it" for i in range(6))
    body = {
        "model": "x",
        "messages": [
            {"role": "user", "content": "list the customers"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "psql"}}],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": rows}]},
        ],
    }
    upstream = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
    headers = {"x-api-key": "test", "content-type": "application/json"}
    raw = json.dumps(body).encode()
    first = mediator.prepare(upstream, "POST", "/anthropic/v1/messages", headers, raw)
    assert first.mode == REWRITE and first.withheld == 1
    sent = json.loads(first.body)["messages"][2]["content"][0]["content"]
    assert sent.startswith(WITHHELD_DUMP)
    again = mediator.prepare(upstream, "POST", "/anthropic/v1/messages", headers, raw)
    assert again.withheld == 0 and json.loads(again.body)["messages"][2]["content"][0]["content"] == sent


def test_the_verifier_counts_outputs_the_plugin_withheld():
    mediator, _ = _mediator()
    notice = f"{WITHHELD_DUMP} 9 personal values (...), more than the 5 allowed in one output.]"
    slots = [(notice, SlotKind.TOOL_RESULT, False), ("hello", SlotKind.USER_TEXT, False)]
    assert mediator.verifier.verify(slots, "cc:s").withheld == 1
    assert mediator.verifier.verify(slots, "cc:s").withheld == 0  # sent already


# --- tools and projects ----------------------------------------------------------------------------


def test_the_requests_say_which_tool_made_each_output():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "x"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": "out"}]}],
            },
        ]
    }
    assert [s.tool for s in anthropic.scan_request(body).slots if s.kind == SlotKind.TOOL_RESULT] == ["Bash"]
    chat = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c", "type": "function", "function": {"name": "query", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c", "content": "rows"},
        ]
    }
    assert [s.tool for s in openai_chat.scan_request(chat).slots if s.kind == SlotKind.TOOL_RESULT] == ["query"]
    responses = {
        "input": [
            {"type": "function_call", "call_id": "r", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "r", "output": "rows"},
            {"type": "custom_tool_call", "call_id": "p", "name": "apply_patch", "input": "x"},
            {"type": "custom_tool_call_output", "call_id": "p", "output": "done"},
        ]
    }
    assert [s.tool for s in openai_responses.scan_request(responses).slots if s.kind == SlotKind.TOOL_RESULT] == [
        "shell",
        "apply_patch",
    ]


def test_production_data_in_the_two_layers():
    repo = ProjectSettings(
        data="strict",
        fields=[FieldRule(name="ral", type="SALARY"), FieldRule(name="id", mask=False)],
        dump_guard=20,
        images="block",
        data_tools=["Bash"],
    )
    config, notices = effective_config(Config(dump_guard=50), repo, None)
    assert {n.key for n in notices} == {"fields.id"}  # the file cannot keep a field in clear
    assert (config.data, config.dump_guard, config.images, config.data_tools) == ("strict", 20, "block", [])
    assert [f.name for f in config.fields] == ["ral"]
    weaker = ProjectSettings(data="normal", dump_guard=80, images="allow")
    config, notices = effective_config(Config(data="strict", dump_guard=50, images="block"), weaker, None)
    assert {n.key for n in notices} == {"data", "dump_guard", "images"}
    assert (config.data, config.dump_guard, config.images) == ("strict", 50, "block")
    mine = ProjectEntry(
        path="/p", data="normal", dump_guard=0, images="allow", fields=[FieldRule(name="stato", mask=False)]
    )
    config, notices = effective_config(Config(), repo, mine)
    assert notices == [n for n in notices if n.key == "fields.id"]
    assert (config.data, config.dump_guard, config.images) == ("normal", None, "allow")
    assert [f.name for f in config.fields] == ["stato", "ral"]  # yours first: the first rule that matches wins


def test_strict_data_mode_in_a_project_follows_its_profile():
    # A repository's max brings strict data mode with it, and its `data: normal` cannot take it away.
    config, notices = effective_config(Config(), ProjectSettings(profile="max"), None)
    assert config.data_mode() == "strict" and notices == []
    config, notices = effective_config(Config(profile="max"), ProjectSettings(data="normal"), None)
    assert config.data_mode() == "strict" and [n.key for n in notices] == ["data"]
    config, notices = effective_config(Config(), ProjectSettings(data="normal"), None)
    assert config.data_mode() == "normal" and notices == []  # the same as yours: nothing to say
    # Your own settings for the project may turn it off; a repository's strict outlasts your profile there.
    mine = ProjectEntry(path="/p", data="normal")
    assert effective_config(Config(profile="max"), None, mine)[0].data_mode() == "normal"
    balanced = ProjectEntry(path="/p", profile="balanced")
    assert effective_config(Config(profile="max"), None, balanced)[0].data_mode() == "normal"
    strict = ProjectSettings(data="strict")
    assert effective_config(Config(profile="max"), strict, balanced)[0].data_mode() == "strict"


# --- the bench's production data -------------------------------------------------------------------


def _db_cases():
    data = yaml.safe_load(CASES.read_text("utf-8"))
    return [c for c in data["cases"] if c["id"].startswith("db-")]


@pytest.mark.parametrize("case", _db_cases(), ids=lambda c: c["id"])
def test_the_bench_db_cases_in_strict_mode(case):
    mediator, session = _mediator(data="strict")
    out = _mask(mediator, session, case["text"])
    for value, _category in case["expect"]:
        assert value not in out.text, value


@pytest.mark.parametrize(
    ("name", "field", "matches"),
    [
        ("orders[].items[].sku", "orders[].items[].sku", True),  # fnmatch read `[]` as a character class
        ("orders[].items[].sku", "orders[].items[].qty", False),
        ("items[].sku", "items[].sku", True),
        ("a?b", "axb", False),  # only `*` is a wildcard
        ("a?b", "a?b", True),
        ("[ab]x", "ax", False),
        ("[ab]x", "[ab]x", True),
        ("note_*", "note_hr", True),
        ("*_id", "customer_id", True),
        ("Stato", "stato", True),  # case does not matter
    ],
)
def test_a_rule_names_its_field_literally_but_for_stars(name, field, matches):
    rule = FieldRule(name=name, mask=False)
    assert (FieldDetector([rule], strict=True)._rule(field) is rule) is matches


# --- reading a large block ---------------------------------------------------------------------------

# The words before RE2 read them: Python's re, case-insensitively (a dotted capital I and a dotless i for i, a long s
# for s, the Kelvin sign for k). What the field detector looks for first must stay what it was.
SECRETISH_BEFORE = re.compile(r"(?i)pass|pwd|secret|token|key|cred|auth|salt|cookie|dsn|bearer")
SECRET_WORDS = ("pass", "pwd", "secret", "token", "key", "cred", "auth", "salt", "cookie", "dsn", "bearer")


def _block(mib: int) -> str:
    line = "2026-10-15 05:12:44 INFO billing worker processed invoice batch 4411 for account 99 in 12 ms\n"
    return (line * (mib * 1048576 // len(line) + 1))[: mib * 1048576]


def test_a_large_block_that_names_no_secret_is_read_quickly_and_holds_up_no_other_thread():
    # Whether a text names a secret is asked of the whole text, which Python's re read in a third of a second for
    # 4 MiB, holding the interpreter all along: every other thread waited, an event loop's too.
    block = _block(4)
    assert SECRETISH_BEFORE.search(block) is None
    detector = FieldDetector()
    contexts = [DetectContext(kind=SlotKind.TOOL_RESULT, tool="Bash"), DetectContext(SlotKind.TOOL_RESULT, field="log")]
    found: list[list[Span]] = []
    done = threading.Event()

    def read() -> None:
        try:
            for _ in range(3):
                found.extend(list(detector.detect(block, ctx)) for ctx in contexts)
        finally:
            done.set()

    gaps = []
    reader = threading.Thread(target=read)
    began = time.perf_counter()
    reader.start()
    while not done.is_set():
        asked = time.perf_counter()
        time.sleep(0.001)
        gaps.append(time.perf_counter() - asked)
    reader.join()
    took = time.perf_counter() - began
    assert found == [[]] * 6
    print(f"\nsix reads of 4 MiB: {took * 1000:.0f} ms, the longest wait of another thread {max(gaps) * 1000:.0f} ms")
    assert took < 1.0  # generous for CI; about 40 ms locally (2.1 s with Python's re)
    assert max(gaps) < 0.1  # about 2 ms locally (0.35 s with Python's re)


def _cased() -> list[str]:
    """Every character that Python's re or RE2 takes for a letter, case-insensitively, wherever its code point is."""
    every = "".join(chr(c) for c in range(sys.maxunicode + 1) if not 0xD800 <= c <= 0xDFFF)
    found = {m.group() for m in re.finditer(r"(?i)[a-z]", every)}
    found |= {m.group() for m in re2.finditer(r"(?i)[a-z]", every)}
    return sorted(found)


def test_the_names_of_a_secret_are_found_as_they_were_whatever_their_case():
    cased = _cased()
    special = {"\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}", "\N{LATIN SMALL LETTER DOTLESS I}", "\N{KELVIN SIGN}"}
    assert special | {"\N{LATIN SMALL LETTER LONG S}"} < set(cased)
    rng = random.Random(7)
    others = [chr(rng.randrange(0x80, 0x30000)) for _ in range(300)] + list(" \t\n\r.-_=:\"'0123456789")
    texts = []
    for word in SECRET_WORDS:
        for at in range(len(word)):
            texts += [word[:at] + c + word[at + 1 :] for c in cased + others]  # one letter in another case, or not one
        texts += ["".join(rng.choice([c, c.upper()]) for c in word) for _ in range(20)]
        texts += [f"user_{word}_hash=1", f"x{word}y", word[:-1], f"{word[:-1]} {word[-1]}"]
    texts += [_block(1), "\N{REPLACEMENT CHARACTER} and a token after it", "", "nothing here at all"]
    texts += [chr(0xD83D) + " half of an emoji, and a token after it", chr(0xD83D)]  # what Python's re read too
    for text in texts:
        assert fields._secretish(text) is (SECRETISH_BEFORE.search(text) is not None), repr(text)


# What the field detector read with Python's re before it read each structure once from start to end. Each one read the
# text again to its end from every place a structure could begin, and a tool's output left open there twenty thousand
# times held the interpreter for seconds, or hours at the detector's two million characters.
ROW_BEFORE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
CELL_BEFORE = re.compile(r"<(t[hd])\b[^>]*>([^<]*)</t[hd]>", re.I)
MD_RULE_BEFORE = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$")


def _html_before(text: str) -> list[fields.Cell]:
    found_cells: list[fields.Cell] = []
    names: list[str] = []
    for row in ROW_BEFORE.finditer(text):
        found = list(CELL_BEFORE.finditer(row.group(1), 0))
        if found and all(m.group(1).lower() == "th" for m in found):
            names = [m.group(2).strip() for m in found]
            continue
        base = row.start(1)
        for index, m in enumerate(found):
            start, end = base + m.start(2), base + m.end(2)
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start:
                found_cells.append(
                    fields.Cell(start, end, names[index] if index < len(names) else f"column {index + 1}")
                )
    return found_cells


def _pattern_before(name: str) -> re.Pattern[str]:
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in name.lower()), re.DOTALL)


def _read_in_time(text: str, bound: float, detector: FieldDetector | None = None, **ctx) -> float:
    detector = detector or FieldDetector()
    began = time.perf_counter()
    list(detector.detect(text, DetectContext(kind=SlotKind.TOOL_RESULT, tool="Bash", **ctx)))
    took = time.perf_counter() - began
    assert took < bound, f"{took:.2f} s"
    return took


@pytest.mark.parametrize(
    "text",
    [
        "token\n" + "<tr>" * 20_000,  # rows opened, never closed: 6.9 s
        "token\n" + "<tr " * 20_000,  # a row's tag never ended: 1.1 s
        "token\n<tr>" + "<td " * 20_000 + "</tr>",  # a cell's tag never ended: 1.1 s
        "token\n<tr>" + "<td " * 100_000 + ">x<p></tr>",  # cells begun in one tag, their text closed by none: 28 s
        "token | a\n" + " " * 20_000 + "x\n---\n",  # a markdown rule of blanks: 3.5 s
        "token | a\n|---|---" + " " * 20_000 + "x\n",  # a markdown rule ending in blanks: 1.5 s
    ],
    ids=["rows-open", "row-tag-open", "cell-tags-open", "cells-unclosed", "rule-of-blanks", "rule-ending-in-blanks"],
)
def test_a_structure_left_open_is_read_once(text):
    _read_in_time(text, 0.25)  # generous for CI; a few milliseconds locally (Python's re: the seconds above)


_LARGEST = """
import json, time
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.fields import FieldDetector
from latet.engine.types import SlotKind
took = []
NL = chr(10)
for before, unit, after in [
    ("token" + NL, "<tr>", ""),
    ("token" + NL, "<tr ", ""),
    ("token" + NL + "<tr>", "<td ", "</tr>"),
    ("token" + NL + "<tr>", "<td ", ">x<p></tr>"),
    ("token | a" + NL, " ", "x" + NL + "---" + NL),
    ("token | a" + NL + "|---|---", " ", "x" + NL),
]:
    text = before + unit * ((2_000_000 - len(before) - len(after)) // len(unit)) + after
    assert len(text) <= 2_000_000
    began = time.perf_counter()
    list(FieldDetector().detect(text, DetectContext(kind=SlotKind.TOOL_RESULT, tool="Bash")))
    took.append(time.perf_counter() - began)
print(json.dumps(took))
"""


def test_a_structure_left_open_at_the_largest_text_the_detector_reads_is_read_in_time():
    # In an interpreter of its own, which a reading that takes hours (Python's re did) cannot hold beyond the timeout.
    done = subprocess.run([sys.executable, "-c", _LARGEST], capture_output=True, text=True, timeout=120, check=True)
    took = json.loads(done.stdout)
    assert max(took) < 2.0, took  # generous for CI; well under a second locally


def test_a_field_rule_of_many_stars_is_matched_in_time_in_proportion_to_its_field():
    # A repository's latet.yaml may set field rules: Python's re tried each way to share a field among a rule's stars.
    for stars, length in ((6, 80), (60, 2_000)):  # 1.7 s with Python's re for the first; it never ended the second
        rule = FieldRule(name="*" * stars + "x")
        detector = FieldDetector([rule])
        _read_in_time("y", 0.1, detector, field="a" * length)
        _read_in_time("| " + "a" * length + " | b |\n|---|---|\n| 1 | 2 |\n", 0.1, detector)


def test_a_long_column_name_is_read_once_for_all_its_rows():
    # Each row's value carried the column's name, which was read again for each: 2.2 s for these thousand rows.
    header = "| token " + "a b " * 1_500 + " | b |\n|---|---|\n"
    _read_in_time(header + "|x|y|\n" * 1_000, 0.25)


def test_html_tables_are_read_as_before():
    rng = random.Random(11)
    pieces = [
        "<tr>", "<TR class='a'>", "<tr", "<tr/>", "</tr>", "</TR>", "<trx>", "<td>", "<TD x=1>", "<th>", "<Th>",
        "<td", "<th", "<tdx>", "</td>", "</th>", "</TD>", "</t", "<t", "<", ">", "/", "<p>", "x", "Ada Byron", " ",
        "\n", "\t", "  y  ", "\N{LATIN SMALL LETTER E WITH ACUTE}", "1",
    ]  # fmt: skip
    for _ in range(4_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randrange(40)))
        assert fields._html(text) == _html_before(text), repr(text)


def test_a_markdown_rule_is_one_as_before():
    letters = " \t|:-x"
    texts = [""]
    for length in range(1, 8):  # every line of seven characters or fewer over these
        texts += ["".join(p) for p in itertools.product(letters, repeat=length)]
    rng = random.Random(13)
    pieces = ["|", "---", "----", ":", " ", "\t", "-", "x", "|---|", ":---:", "  |", "| :--- |"]
    texts += ["".join(rng.choice(pieces) for _ in range(rng.randrange(2, 16))) for _ in range(20_000)]
    for text in texts:
        assert (fields._MD_RULE.match(text) is None) is (MD_RULE_BEFORE.match(text) is None), repr(text)


def test_a_field_rule_matches_its_fields_as_before():
    rng = random.Random(17)
    backslash = chr(92)
    in_names = [
        "*", "*", "a", "b", "A", ".", "[", "]", "[]", "(", ")", "?", "+", "^", "$", "|", "{", "}", " ", "-", "_",
        backslash, "\n", "\N{LATIN SMALL LETTER E WITH ACUTE}", "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}",
        "\N{LATIN SMALL LETTER SHARP S}", "\N{KELVIN SIGN}", "\x00",
    ]  # fmt: skip
    # A rule's name is text the settings took, never half of a character; a field's may hold one (Python's re read it).
    in_fields = [*in_names[2:], "ab", "items[]", "x.y", "\N{LATIN SMALL LETTER DOTLESS I}", "i", chr(0xD800)]
    for _ in range(20_000):
        name = "".join(rng.choice(in_names) for _ in range(rng.randrange(1, 8)))
        field = "".join(rng.choice(in_fields) for _ in range(rng.randrange(8)))
        rule = FieldRule(name=name, mask=False)
        before = _pattern_before(name)
        expected = before.fullmatch(field.lower()) or before.fullmatch(fields._last(field).lower())
        assert (FieldDetector([rule], strict=True)._rule(field) is rule) is (expected is not None), (name, field)


# --- what is kept of a field's name ----------------------------------------------------------------

# Keys that are values: an address, another address a field rule of yours names, a name before a secret's word.
NAMED_BY_VALUES = {
    "emily.carter@acme.com": "VIP customer since 2019",
    "daniel.okafor@acme.com": "gold plan member",
    "laura carter password": "hunter2222",
}
VALUES_IN_KEYS = ("emily", "carter", "acme", "daniel", "okafor", "laura")


def _rules_kept(path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [rule for (rule,) in conn.execute("SELECT rule FROM entities") if rule is not None]


def test_the_vault_keeps_a_fields_kind_and_never_its_name_which_can_be_a_value(tmp_path):
    """The rule that finds a value by its field names the field, and a field's name can be a value itself (a JSON key
    that is an address): the vault keeps each value's rule in clear beside its sealed original. It keeps the field's
    kind alone; what a text was masked with still names the field (the dashboard proposes its columns by it), as it
    was sent: a key that is an address is masked as a value, and named by its fake."""
    config = Config(
        identity=IdentityConfig(enabled=False), data="strict", fields=[FieldRule(name="daniel.okafor@acme.com")]
    )
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        session = rt.built.engine.session("s")
        done = rt.mediator.mask_value(json.dumps(NAMED_BY_VALUES), SlotKind.TOOL_RESULT, session, MaskReport())
        address = rt.built.vault.by_original("emily.carter@acme.com")
        assert address is not None and address.surrogate in done.text and "emily.carter@acme.com" not in done.text
        assert {r.rule for r in done.replacements} == {
            f"strict:{address.surrogate}", "field:daniel.okafor@acme.com", "field-secret:laura carter password",
            "pii:email",
        }  # fmt: skip
        kept = sorted(e.rule or "" for e in rt.built.vault.entities())
        assert [rule for rule in kept if rule != "pii:email"] == ["field", "field-secret:password", "strict"]
        assert sorted(_rules_kept(tmp_path / "data" / "latet.db")) == kept
    finally:
        rt.close()


def test_a_rule_kept_with_a_fields_name_before_is_kept_by_the_fields_kind_once_the_vault_opens(tmp_path):
    path = tmp_path / "latet.db"
    rules = ["strict:emily.carter@acme.com", "field:daniel.okafor@acme.com", "field-secret:laura carter password"]
    for n, rule in enumerate(rules):
        entity = Entity("e" + str(n) * 19, "DATA", EntityClass.STRUCT, f"value {n}", f"fake {n}", 0, time.time(), rule)
        Store(path, ROOT_KEY).save_entity(replace(entity, rule=None))
        with sqlite3.connect(path) as conn:  # kept before the vault cut a field's name: by hand
            conn.execute("UPDATE entities SET rule = ? WHERE id = ?", (rule, entity.id))
    store = Store(path, ROOT_KEY)
    assert sorted(e.rule or "" for e in store.load_entities()) == ["field", "field-secret:password", "strict"]
    assert not any(value in rule for rule in _rules_kept(path) for value in VALUES_IN_KEYS)


def test_every_way_into_the_vault_keeps_a_fields_kind_alone(tmp_path):
    named = Entity("e" * 20, "DATA", EntityClass.STRUCT, "VIP", "Kqz", 0, time.time(), "strict:emily.carter@acme.com")
    secret = replace(named, id="f" * 20, original="hunter2222", surrogate="x" * 10, rule=None)
    assert [e.rule for e in Vault(ROOT_KEY, [named]).entities()] == ["strict"]  # loaded
    vault = Vault(ROOT_KEY)
    vault.learn(named)  # kept by another process
    assert vault.get(named.id).rule == "strict"
    path = tmp_path / "latet.db"
    store = Store(path, ROOT_KEY)
    store.save_entity(named)  # given as it is, as a backup's restore gives it
    store.save_entity(secret)
    store.save_rule(replace(secret, rule="field-secret:laura carter password"))
    assert sorted(_rules_kept(path)) == ["field-secret:password", "strict"]
    with sqlite3.connect(path) as conn:  # written meanwhile by an earlier Latet
        conn.execute("UPDATE entities SET rule = 'field:daniel.okafor@acme.com' WHERE id = ?", (named.id,))
    assert {e.id: e.rule for e in store.load_entities()}[named.id] == "field"


# --- a document's values under one long key --------------------------------------------------------

_LONG_KEY = """
import asyncio, json, sys, time
from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.masker import MaskReport
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.mediate import Mediator

data, ending = sys.argv[1], sys.argv[2]
built = build(Config(identity=IdentityConfig(enabled=False), data=data), Vault(bytes(32)))
mediator = Mediator(built.engine, built.verifier)
session = built.engine.session("s")
key = "word " * 40_000 + ending  # 200 KB of words: a key's path is read word by word
doc = json.dumps({key: [f"value {n:04d} " + "of the tool's long output, " * 4 for n in range(2_000)]})  # 400 KB in all
assert len(key) >= 200_000 and len(doc) >= 400_000

async def main():
    gaps, ticking = [], True
    async def ticker():
        last = time.perf_counter()
        while ticking:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    beating = asyncio.ensure_future(ticker())
    began = time.perf_counter()
    done = await asyncio.to_thread(mediator.mask_value, doc, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Bash")
    took = time.perf_counter() - began
    ticking = False
    await beating
    labels = max(len(r.rule or "") for r in done.replacements)
    print(json.dumps({"took": took, "gap": max(gaps), "masked": len(done.replacements), "labels": labels}))

asyncio.run(main())
"""


@pytest.mark.parametrize(
    ("data", "ending", "masked"),
    [("strict", "note", 2_000), ("normal", "secret_password", 2_000)],
    ids=["strict-data-mode", "a-field-named-like-a-secret"],
)
def test_a_documents_values_under_one_long_key_cost_what_their_values_do(data, ending, masked):
    # The key's path was made, cut, hashed into the cache's key and read word by word again for each value under it:
    # 45 ms a value for these 200 KB, minutes for the 2,000. In an interpreter of its own, which a timeout ends.
    done = subprocess.run(
        [sys.executable, "-c", _LONG_KEY, data, ending], capture_output=True, text=True, timeout=180, check=True
    )
    found = json.loads(done.stdout)
    assert found["masked"] == masked
    assert found["took"] < 20, found  # generous for CI; 3 to 6 s locally, as long as with a key of a few letters
    assert found["gap"] < 0.1, found  # the event loop that handed the document over went on meanwhile
    assert found["labels"] <= 220, found  # a value's rule names a field a rule could name, never one 200 KB long


def test_a_keys_path_is_made_once_for_every_value_under_it():
    paths: list[str] = []
    doc = json.dumps({"k" * 1000: [{"inner": n, "deep": [[n]]} for n in range(50)]})
    map_json_values(doc, lambda value, path, number: paths.append(path) or value, numbers=True)
    inner = [p for p in paths if p.endswith(".inner")]
    deep = [p for p in paths if p.endswith(".deep[][]")]
    assert len(inner) == len(deep) == 50
    assert all(p is inner[0] for p in inner) and all(p is deep[0] for p in deep)  # one string, not fifty copies


def test_what_a_path_costs_is_paid_once_for_every_value_under_it(monkeypatch):
    """Cutting a value's field from its path, its digest in the cache's key, what its name says: each once for the
    document's forty values, where each cost them the key's length (in C, or in Python for what the name says)."""
    calls: Counter[str] = Counter()
    cut, read = mediate_module._field, FieldDetector._read
    monkeypatch.setattr(mediate_module, "_field", lambda path: calls.update(["cut"]) or cut(path))
    monkeypatch.setattr(FieldDetector, "_read", lambda self, field: calls.update(["read"]) or read(self, field))
    digest = hashlib.blake2b
    tagged = SimpleNamespace(blake2b=lambda *a, **k: calls.update(["tag"]) or digest(*a, **k), sha256=hashlib.sha256)
    monkeypatch.setattr(pipeline_module, "hashlib", tagged)
    mediator, session = _mediator(data="strict")
    doc = json.dumps([{"k" * 500: [f"value number {n} of the tool" for n in range(40)]}])  # in an array: a path to cut
    assert len(_mask(mediator, session, doc).replacements) == 40
    assert calls == {"cut": 1, "read": 1, "tag": 1}


def test_a_field_longer_than_a_rule_can_name_is_named_by_its_kind_in_a_values_rule():
    mediator, session = _mediator(data="strict")
    for length, strict, secret in ((200, None, None), (201, "strict", "field-secret:password")):
        key = "n" * (length - len("_note")) + "_note"
        secret_key = "n" * (length - len("_password")) + "_password"
        done = _mask(mediator, session, json.dumps({key: "a value of the tool", secret_key: "hunter2222"}))
        rules = sorted(r.rule for r in done.replacements)
        assert rules == sorted([strict or f"strict:{key}", secret or f"field-secret:{secret_key}"]), length


def _map_json_values_before(raw, transform, numbers=False, keys=None):
    """``map_json_values`` before it made a key's path once for every value under it: the oracle (its keys masked as
    they are now, each before the values under it)."""
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return transform(raw, "", False)
    changed = False

    def walk(v, path):
        nonlocal changed
        if isinstance(v, str):
            out = transform(v, path, False)
            changed = changed or out != v
            return out
        if numbers and isinstance(v, int | float) and not isinstance(v, bool):
            text = json.dumps(v)
            out = transform(text, path, True)
            if out == text:
                return v
            changed = True
            try:
                parsed = json.loads(out)
            except ValueError:
                return out
            return parsed if isinstance(parsed, int | float) and not isinstance(parsed, bool) else out
        if isinstance(v, dict):
            others = "\n".join(v)  # the object's keys, a fake drawn for one none of them
            named = {k: keys(k, path, others) if keys is not None else k for k in v}
            changed = changed or any(new != k for k, new in named.items())
            return {named[k]: walk(x, f"{path}.{k}" if path else str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{path}[]") for x in v]
        return v

    new = walk(doc, "")
    if not changed:
        return raw
    return json.dumps(new, ensure_ascii=False, separators=(",", ":"))


def _documents(rng: random.Random, count: int) -> list[str]:
    keys = ["id", "name", "email", "password", "api_key", "note", "items", "stato", "customer_id", "db.password"]
    values = ["Marta Bianchi", "marta@acme.it", "hunter2222", "41", "3.5", "SPEDITO", "ok", "", "x" * 30, None, True]

    def make(depth: int):
        if depth > 2 or rng.random() < 0.3:
            return rng.choice(values)
        if rng.random() < 0.5:
            return {
                rng.choice(keys) + rng.choice(["", "_2", ".x"]): make(depth + 1) for _ in range(rng.randrange(1, 4))
            }
        return [make(depth + 1) for _ in range(rng.randrange(1, 4))]

    return [json.dumps({rng.choice(keys): make(0) for _ in range(rng.randrange(1, 4))}) for _ in range(count)]


@pytest.mark.parametrize("data", ["normal", "strict"])
def test_a_documents_values_are_masked_as_they_were_with_their_paths_read_once(monkeypatch, data):
    rng = random.Random(23)
    corpus = [PSQL, *_documents(rng, 150)]
    now, _ = _mediator(data=data)
    before, _ = _mediator(data=data)
    found_now = [_mask(now, now.engine.session("a"), doc) for doc in corpus]
    monkeypatch.setattr("latet.gateway.mediate.map_json_values", _map_json_values_before)
    monkeypatch.setattr("latet.engine.document.reading", contextlib.nullcontext)
    found_before = [_mask(before, before.engine.session("a"), doc) for doc in corpus]
    for doc, a, b in zip(corpus, found_now, found_before, strict=True):
        assert a.text == b.text, doc
        assert [(r.start, r.end, r.etype, r.eclass, r.rule) for r in a.replacements] == [
            (r.start, r.end, r.etype, r.eclass, r.rule) for r in b.replacements
        ], doc
