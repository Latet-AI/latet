"""Strict data mode for files and web addresses: a file a read tool returns counts as data when its path matches a
rule of `data_files`, a web tool's result when its address's host matches one of `data_hosts`; such a file's values
are found by its format (JSON, tables, .env and properties, YAML, a named markdown file's tables only)."""

import json

import pytest
from pydantic import ValidationError

from latet.config import Config, ProjectEntry, ProjectSettings
from latet.engine.build import build
from latet.engine.detectors.fields import strict_covers
from latet.engine.detectors.files import file_cells, numbered_view
from latet.engine.masker import MaskReport
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.gateway.mediate import Mediator
from latet.project_file import REPO_KEYS
from latet.projects import effective_config
from latet.protection import loosened
from latet.protocols import anthropic, openai_chat, openai_responses
from latet.server.shared import UNION_KEYS
from latet.storage.exchanges import BlockRecord, block_ref, ref_parts
from latet.transfer import export_rules, merge_rules, read_rules

from ..conftest import ROOT_KEY


def _mediator(**config):
    built = build(Config(**config), Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier), built.engine.session("s")


def _numbered(text: str, arrow: bool = False) -> str:
    """``text`` as Claude Code's Read tool returns it: each line after its number."""
    mark = "\u2192" if arrow else "\t"
    return "\n".join(f"{n:>6}{mark}{line}" for n, line in enumerate(text.split("\n"), 1))


def _found(text: str, path: str) -> set[tuple[str, str]]:
    return {(cell.field, text[cell.start : cell.end]) for cell in file_cells(text, path)}


# --- which files and addresses count -----------------------------------------------------------------


def test_a_file_counts_as_data_when_its_path_matches_a_rule_of_data_files():
    assert not strict_covers("Read", (), "/work/acme/data.json")  # files alone are never data
    assert strict_covers("Read", (), "/work/acme/data.json", files=["*.json"])
    assert strict_covers("Read", (), "/work/acme/DATA.JSON", files=["*.json"])  # case does not matter
    assert strict_covers("Read", (), "C:\\work\\acme\\exports\\q1\\orders.csv", files=["exports/**"])
    assert strict_covers("Read", (), "/work/acme/exports/q1/orders.csv", files=["**/*.csv"])
    assert strict_covers("Read", (), "/work/acme/.env.local", files=[".env*"])
    assert not strict_covers("Read", (), "/work/acme/data.json", files=["*.csv"])
    assert not strict_covers("Read", (), "/work/acme/report/exports.txt", files=["exports/**"])
    assert not strict_covers("Read", (), None, files=["*"])  # no path known: not a file a rule can name
    # A search's or a listing's lines are not the file: never data.
    for tool in ("Grep", "Glob", "LS"):
        assert not strict_covers(tool, (), "/work/acme/data.json", files=["*.json"])
    # data_tools says which tools' outputs count; it does not take a file a rule names away.
    assert strict_covers("Read", ["mcp__crm__*"], "/work/acme/data.json", files=["*.json"])


def test_a_markdown_file_counts_only_when_a_rule_names_markdown():
    for path in ("/work/acme/README.md", "/work/acme/CLAUDE.md", "/work/acme/docs/guide.markdown"):
        assert not strict_covers("Read", (), path, files=["*", "**/*", "docs/**"])
    assert strict_covers("Read", (), "/work/acme/docs/prices.md", files=["docs/*.md"])
    assert strict_covers("Read", (), "/work/acme/prices.MD", files=["*.md"])


def test_a_web_result_counts_as_data_when_its_host_matches_a_rule_of_data_hosts():
    hosts = ["api.internal.example.com", "*.salesforce.com"]
    assert strict_covers("WebFetch", (), "https://api.internal.example.com/v1/orders?page=2", hosts=hosts)
    assert strict_covers("WebFetch", (), "https://EU1.Salesforce.com:443/x", hosts=hosts)
    assert not strict_covers("WebFetch", (), "https://example.org/salesforce.com", hosts=hosts)
    assert not strict_covers("WebFetch", (), "https://example.org/", hosts=hosts)
    assert not strict_covers("WebFetch", (), "not an address", hosts=["*"])
    assert not strict_covers("WebSearch", (), None, hosts=["*"])  # a search has no address of its own
    assert strict_covers("WebFetch", ["mcp__crm__*"], "https://eu1.salesforce.com/", hosts=hosts)
    assert not strict_covers("WebFetch", ["mcp__crm__*"], "https://example.org/", hosts=hosts)


# --- the file without its line numbers ----------------------------------------------------------------


@pytest.mark.parametrize("arrow", [False, True])
def test_a_read_files_lines_are_seen_without_their_numbers(arrow):
    file = '{\n  "name": "Ada Byron"\n}'
    text = _numbered(file, arrow) + "\n\n<system-reminder>\nWhenever you read a file...\n</system-reminder>\n"
    view = numbered_view(text)
    assert view.text == file
    at = view.text.index("Ada Byron")
    assert text[view.original(at) : view.original(at + len("Ada Byron"))] == "Ada Byron"


def test_a_text_without_line_numbers_is_seen_as_it_is():
    view = numbered_view("name,email\nAda,ada@example.com")
    assert view.text == "name,email\nAda,ada@example.com"
    assert view.original(5) == 5


# --- each format's values ------------------------------------------------------------------------------


def test_every_value_of_a_json_file_with_its_path_and_never_a_key():
    text = (
        '{"customer": {"name": "Ada Byron", "tier": "gold"},\n "orders": [{"sku": "A-100", "qty": 1200}],\n'
        ' "tags": ["vip", "eu"], "ok": true, "none": null, "say": "a \\"quoted\\" word"}'
    )
    assert _found(text, "/p/customer.json") == {
        ("customer.name", "Ada Byron"),
        ("customer.tier", "gold"),
        ("orders[].sku", "A-100"),
        ("orders[].qty", "1200"),
        ("tags[]", "vip"),
        ("tags[]", "eu"),
        ("say", 'a \\"quoted\\" word'),
    }


def test_each_line_of_a_json_lines_file():
    text = '{"name": "Ada Byron"}\n{"name": "Grace Hopper"}\n'
    assert _found(text, "/p/people.jsonl") == {("name", "Ada Byron"), ("name", "Grace Hopper")}


def test_a_file_named_json_that_is_not_json_is_read_for_structures():
    assert _found("name,email\nAda,ada@example.com", "/p/broken.json") == {
        ("name", "Ada"),
        ("email", "ada@example.com"),
    }


def test_the_cells_of_a_table_file():
    assert _found("name,email\nAda,ada@example.com\n", "/p/people.csv") == {
        ("name", "Ada"),
        ("email", "ada@example.com"),
    }
    assert _found("name\temail\nAda\tada@example.com\n", "/p/people.tsv") == {
        ("name", "Ada"),
        ("email", "ada@example.com"),
    }


def test_the_values_of_an_env_file():
    text = 'DB_HOST=db.internal\n# a comment\nexport REGION="eu-west-1"\nEMPTY=\n  OWNER = Ada Byron\n'
    assert _found(text, "/p/.env.production") == {
        ("DB_HOST", "db.internal"),
        ("REGION", "eu-west-1"),
        ("OWNER", "Ada Byron"),
    }


def test_the_values_of_a_properties_file():
    text = "app.owner = Ada Byron\napp.tier: gold\n! a comment\n# another\n[section]\nregion=eu\n"
    assert _found(text, "/p/app.properties") == {("app.owner", "Ada Byron"), ("app.tier", "gold"), ("region", "eu")}


def test_the_scalars_of_a_yaml_file_and_never_its_keys():
    text = (
        "customer:\n"
        "  name: Ada Byron   # the owner\n"
        '  tier: "gold"\n'
        "servers:\n"
        "  - host: db1.internal\n"
        "    port: 5432\n"
        "  - db2.internal\n"
        "notes: |\n"
        "  first line\n"
        "  second line\n"
        "empty:\n"
        "anchor: &base\n"
        "# a comment: here\n"
        "---\n"
        "url: 'https://crm.example.com/a#b'\n"
    )
    assert _found(text, "/p/config.yaml") == {
        ("customer.name", "Ada Byron"),
        ("customer.tier", "gold"),
        ("servers[].host", "db1.internal"),
        ("servers[].port", "5432"),
        ("servers[]", "db2.internal"),
        ("notes", "first line"),
        ("notes", "second line"),
        ("url", "https://crm.example.com/a#b"),
    }


def test_a_named_markdown_files_tables_only_never_its_prose():
    text = (
        "# Prices\n\nThe owner is Ada Byron, region=eu tier=gold.\n\n"
        "| item | price |\n|---|---|\n| widget | 12.50 |\n\nname,email\nAda,ada@example.com\n"
    )
    assert _found(text, "/p/docs/prices.md") == {("item", "widget"), ("price", "12.50")}


def test_a_file_of_another_kind_is_read_as_json_when_it_is_json_else_for_structures():
    assert _found('{"name": "Ada Byron"}', "/p/exports/out.txt") == {("name", "Ada Byron")}
    assert _found("name,email\nAda,ada@example.com", "/p/exports/out.txt") == {
        ("name", "Ada"),
        ("email", "ada@example.com"),
    }


# --- the settings ----------------------------------------------------------------------------------------------


def test_the_rules_are_bounded_and_kept_once_without_blanks():
    assert Config(data_files=[" *.json ", "*.json", "", "exports/**"]).data_files == ["*.json", "exports/**"]
    with pytest.raises(ValidationError, match="at most 200 rules"):
        Config(data_files=[f"*.{n}" for n in range(201)])
    with pytest.raises(ValidationError, match="entry 2: longer than 200 characters"):
        Config(data_hosts=["a.example.com", "a" * 201])
    assert ProjectSettings(data_hosts=["*.salesforce.com"]).data_hosts == ["*.salesforce.com"]


def test_a_repository_file_and_your_project_settings_add_files_and_addresses():
    repo = ProjectSettings(data_files=["exports/**"], data_hosts=["*.salesforce.com"])
    mine = ProjectEntry(path="/p", data_files=["*.json"], data_hosts=["api.internal.example.com"])
    config, notices = effective_config(Config(data_files=["*.csv"], data_hosts=["*.salesforce.com"]), repo, mine)
    assert notices == []  # adding protects: a repository file may add them too
    assert config.data_files == ["*.csv", "exports/**", "*.json"]  # the global ones, the repository's, yours
    assert config.data_hosts == ["*.salesforce.com", "api.internal.example.com"]
    assert "data_files" in REPO_KEYS and "data_hosts" in REPO_KEYS


def test_adding_a_rule_protects_and_removing_one_lowers_protection():
    every = Config(data="strict", data_files=["*.json", "*.csv"], data_hosts=["*.salesforce.com"])
    assert loosened(Config(data="strict"), every) == []
    assert loosened(every, every.model_copy(update={"data_files": ["*.json"]})) == [
        "strict data mode: covers fewer files"
    ]
    assert loosened(every, every.model_copy(update={"data_hosts": []})) == [
        "strict data mode: covers fewer web addresses"
    ]
    off = Config(data="normal", data_files=["*.json"])  # a file named is data with strict data mode off too
    assert loosened(off, off.model_copy(update={"data_files": []})) == ["strict data mode: covers fewer files"]


def test_a_rules_file_brings_files_and_addresses_as_protection():
    imported, problems = read_rules("data_files: ['*.json']\ndata_hosts: ['*.salesforce.com']\n")
    assert problems == []
    merged = merge_rules(Config(data="strict"), imported)
    assert merged.protective.data_files == ["*.json"] and merged.protective.data_hosts == ["*.salesforce.com"]
    assert merged.report.held == []  # added at once: nothing waits for approval
    text, _ = export_rules(merged.protective)
    assert "data_files:" in text and "data_hosts:" in text


def test_a_server_unites_what_it_sets_for_everyone_with_each_persons_own():
    assert "data_files" in UNION_KEYS and "data_hosts" in UNION_KEYS  # for an edition that shares rules (E1, E6)


# --- where an output came from -------------------------------------------------------------------------------


def _sources(scan) -> list[tuple[str | None, str | None]]:
    return [(slot.tool, slot.source) for slot in scan.slots if slot.kind == SlotKind.TOOL_RESULT]


def test_an_anthropic_request_says_which_file_or_address_each_output_came_from():
    calls = [
        {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/work/acme/data.json"}},
        {"type": "tool_use", "id": "n", "name": "NotebookRead", "input": {"notebook_path": "/work/acme/a.ipynb"}},
        {
            "type": "tool_use",
            "id": "w",
            "name": "WebFetch",
            "input": {"url": "https://crm.example.com/a", "prompt": "x"},
        },
        {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "cat data.json"}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "r", "content": "     1\t{}"},
        {"type": "tool_result", "tool_use_id": "n", "content": [{"type": "text", "text": "cells"}]},
        {"type": "tool_result", "tool_use_id": "w", "content": "page"},
        {"type": "tool_result", "tool_use_id": "b", "content": "{}"},
    ]
    body = {"messages": [{"role": "assistant", "content": calls}, {"role": "user", "content": results}]}
    assert _sources(anthropic.scan_request(body)) == [
        ("Read", "/work/acme/data.json"),
        ("NotebookRead", "/work/acme/a.ipynb"),
        ("WebFetch", "https://crm.example.com/a"),
        ("Bash", None),
    ]


def test_an_openai_request_says_which_file_or_address_each_output_came_from():
    arguments = '{"path": "/work/acme/rows.csv"}'
    chat = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": arguments}},
                    {"id": "d", "type": "function", "function": {"name": "query", "arguments": "not json"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c", "content": "rows"},
            {"role": "tool", "tool_call_id": "d", "content": "rows"},
        ]
    }
    assert _sources(openai_chat.scan_request(chat)) == [("read_file", "/work/acme/rows.csv"), ("query", None)]
    responses = {
        "input": [
            {
                "type": "function_call",
                "call_id": "f",
                "name": "web_fetch",
                "arguments": '{"url": "https://a.example/x"}',
            },
            {"type": "function_call_output", "call_id": "f", "output": "page"},
            {"type": "function_call", "call_id": "s", "name": "shell", "arguments": '{"command": ["ls"]}'},
            {"type": "function_call_output", "call_id": "s", "output": "a.json"},
        ]
    }
    assert _sources(openai_responses.scan_request(responses)) == [
        ("web_fetch", "https://a.example/x"),
        ("shell", None),
    ]


def test_the_history_keeps_an_outputs_source_beside_its_tool_and_reads_older_references():
    read = BlockRecord("b", "$.messages[2]", "tool_result", False, True, tool="Read", source="/work/acme/a.json")
    assert ref_parts(block_ref(read))[4:] == ("Read", "/work/acme/a.json")
    fetched = BlockRecord("b", "$", "tool_result", False, True, source="https://crm.example.com/a")
    assert ref_parts(block_ref(fetched))[4:] == (None, "https://crm.example.com/a")
    assert ref_parts(["b", "$", 0, 1])[4:] == (None, None)  # recorded before tools were kept
    assert ref_parts(["b", "$", 0, 1, "Bash"])[4:] == ("Bash", None)  # before sources were


# --- what leaves --------------------------------------------------------------------------------------------

CUSTOMER = '{\n  "customer": {\n    "name": "Ada Byron",\n    "tier": "platinum"\n  },\n  "sku": "A-100-XL"\n}'


def test_strict_masks_every_value_of_a_json_file_a_rule_names_and_keeps_its_lines():
    numbered = _numbered(CUSTOMER)
    path = "/work/acme/exports/customer.json"
    mediator, session = _mediator(data="strict", data_files=["*.json"])
    out = mediator.mask_value(numbered, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Read", path=path)
    for value in ("Ada Byron", "platinum", "A-100-XL"):
        assert value not in out.text
    sent, read = out.text.split("\n"), numbered.split("\n")
    assert len(sent) == len(read)
    for line, original in zip(sent, read, strict=True):  # every line keeps its number, its keys and its signs
        assert line.split("\t")[0] == original.split("\t")[0]
        assert line.count('"') == original.count('"')
    assert '"customer": {' in out.text and '"tier": "' in out.text
    assert {r.eclass for r in out.replacements} == {EntityClass.STRUCT}
    assert out.exact  # each value where it was: the dashboard shows each in its place


def test_without_a_rule_naming_it_a_file_leaves_as_today():
    numbered = _numbered(CUSTOMER)
    for config in ({"data": "strict"}, {"data": "strict", "data_files": ["*.csv"]}, {"data_files": ["*.csv"]}):
        mediator, session = _mediator(**config)
        out = mediator.mask_value(
            numbered, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Read", path="/work/acme/customer.json"
        )
        assert "platinum" in out.text and "A-100-XL" in out.text, config


def test_a_file_or_an_address_a_rule_names_is_data_with_strict_data_mode_off_too():
    # Balanced, the default, has strict data mode off: naming a file or an address is enough to mask it as data.
    mediator, session = _mediator(data_files=["*.json"], data_hosts=["api.internal.example.com"])
    read = mediator.mask_value(
        _numbered(CUSTOMER), SlotKind.TOOL_RESULT, session, MaskReport(), tool="Read", path="/work/acme/customer.json"
    )
    assert "platinum" not in read.text and "A-100-XL" not in read.text
    page = '{"orders": [{"customer": "Ada Byron", "status": "OVERDUE"}]}'
    fetched = mediator.mask_value(
        page, SlotKind.TOOL_RESULT, session, MaskReport(), tool="WebFetch", path="https://api.internal.example.com/o"
    )
    assert "OVERDUE" not in fetched.text
    # Any other tool's output stays as strict data mode off leaves it.
    rows = mediator.mask_value(page, SlotKind.TOOL_RESULT, session, MaskReport(), tool="run_sql")
    assert "OVERDUE" in rows.text


def test_strict_masks_a_web_result_from_a_host_a_rule_names():
    page = '{"orders": [{"customer": "Ada Byron", "status": "OVERDUE", "balance": 98765}]}'
    url = "https://api.internal.example.com/v1/orders"
    mediator, session = _mediator(data="strict", data_hosts=["api.internal.example.com"])
    out = mediator.mask_value(page, SlotKind.TOOL_RESULT, session, MaskReport(), tool="WebFetch", path=url)
    assert "OVERDUE" not in out.text and "Ada Byron" not in out.text
    assert "98765" not in out.text  # its numbers too, as strict data mode masks a covered document's
    other = mediator.mask_value(
        page, SlotKind.TOOL_RESULT, session, MaskReport(), tool="WebFetch", path="https://example.org/orders"
    )
    assert "OVERDUE" in other.text


# --- the final review's findings ---------------------------------------------------------------------------

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"


@pytest.mark.parametrize(
    ("tool", "source"),
    [
        ("Read", "/work/acme/node_modules/pkg/index.js"),
        ("Read", "/work/acme/package-lock.json"),
        ("Read", "/work/acme/.venv/lib/python3.12/site-packages/x.py"),
        ("Read", "/work/acme/logo.svg"),
        ("Grep", "node_modules"),
        ("WebFetch", "https://example.org/a.svg"),
    ],
)
def test_a_secret_is_masked_wherever_the_output_came_from(tool, source):
    """Where an output came from is no file a secret scanner's own list of paths to skip applies to."""
    mediator, session = _mediator()
    out = mediator.mask_value(f"token = {GH_PAT}", SlotKind.TOOL_RESULT, session, MaskReport(), tool=tool, path=source)
    assert GH_PAT not in out.text


def _mask_file(text: str, path: str, **config) -> str:
    mediator, session = _mediator(**{"data": "strict", **config})
    return mediator.mask_value(text, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Read", path=path).text


def _numbered_from(text: str, first: int) -> str:
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(text.split("\n"), first))


def test_a_named_markdown_file_still_has_its_secrets_masked_by_their_names():
    text = _numbered(
        "# Notes\n\ndb_password=Hunter2Hunter2 db_user=ada\n\n| item | price |\n|---|---|\n| widget | 12.50 |"
    )
    out = _mask_file(text, "/work/acme/docs/notes.md", data_files=["*.md"])
    assert "Hunter2Hunter2" not in out and "widget" not in out


def test_a_table_file_read_from_a_later_line_has_every_cell_masked():
    rows = "\n".join(f"Customer{n} Smith,Gold{n}Tier,ORD-00{n},Milano" for n in range(501, 504))
    out = _mask_file(_numbered_from(rows, 501), "/work/acme/exports/orders.csv", data_files=["*.csv"])
    for n in range(501, 504):
        assert f"Customer{n} Smith" not in out and f"ORD-00{n}" not in out


def test_a_table_file_with_symbols_in_its_header_or_a_ragged_row_has_every_cell_masked():
    text = _numbered(
        "Order #,Amount ($),Customer\nORD-1001,1250.50,Ada Byron\nORD-1002,80.00,Grace Hopper,extra cell\n"
        'ORD-1003,430.00,"Turing, Alan"'
    )
    out = _mask_file(text, "/work/acme/exports/orders.csv", data_files=["*.csv"])
    for value in ("ORD-1001", "1250.50", "Ada Byron", "Grace Hopper", "extra cell", "ORD-1003", "Turing, Alan"):
        assert value not in out, value
    assert "Order #,Amount ($),Customer" in out  # the header stays


def test_a_json_file_read_in_part_has_every_value_masked():
    whole = (
        '{\n  "customers": [\n    {\n      "contact person": "Ada Byron",\n      "tags": [\n'
        '        "vip-customer",\n        "eu-region"\n      ],\n      "balance": 125000\n    },\n'
        '    {\n      "contact person": "Grace Hopper",\n      "note": "cut here and never closed'
    )
    out = _mask_file(_numbered(whole), "/work/acme/exports/customers.json", data_files=["*.json"])
    for value in ("Ada Byron", "vip-customer", "eu-region", "125000", "Grace Hopper", "cut here and never closed"):
        assert value not in out, value
    assert '"contact person": "' in out  # the keys stay
    later = _numbered_from('      "contact person": "Alan Turing",\n      "tags": ["uk-region"]', 2001)
    out = _mask_file(later, "/work/acme/exports/customers.json", data_files=["*.json"])
    assert "Alan Turing" not in out and "uk-region" not in out


def test_a_deeply_nested_json_file_is_read_in_bounded_time_and_memory():
    import time
    import tracemalloc

    deep = "[" * 100_000 + '"Ada Byron"' + "]" * 100_000
    tracemalloc.start()
    started = time.perf_counter()
    found = file_cells(deep, "/p/deep.json")
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert [deep[c.start : c.end] for c in found] == ["Ada Byron"]
    assert elapsed < 3 and peak < 64 * 1024 * 1024, (elapsed, peak)


def test_a_yaml_line_of_long_blanks_is_read_in_linear_time():
    import time

    text = "key" + " " * 200_000 + "\nname: Ada Byron\n"
    started = time.perf_counter()
    found = _found(text, "/p/config.yaml")
    assert time.perf_counter() - started < 1
    assert ("name", "Ada Byron") in found


def test_a_named_file_past_the_size_read_for_structures_has_each_line_masked_whole():
    from latet.engine.detectors.fields import _MAX_TEXT

    line = "Ada Byron;platinum;A-100-XL"
    text = "\n".join([line] * (_MAX_TEXT // len(line) + 10))
    found = file_cells(text, "/p/exports/big.txt")
    assert len(found) == text.count("\n") + 1
    assert all(text[c.start : c.end] == line for c in found)


@pytest.mark.parametrize(
    "text",
    [
        "42\tAda Byron\tplatinum\n17\tGrace Hopper\tgold",  # a headerless TSV whose first column is a number
        "     1\tfirst line\nsecond line, not numbered",
    ],
)
def test_a_text_is_taken_as_numbered_lines_only_when_its_lines_are(text):
    assert numbered_view(text).text == text


def test_a_numbered_file_of_one_line_and_opencodes_numbered_read():
    assert numbered_view("     1\tonly line").text == "only line"
    opencode = "<file>\n00001| name,email\n00002| Ada,ada@example.com\n\n(End of file - total 2 lines)\n</file>"
    view = numbered_view(opencode)
    assert view.text == "name,email\nAda,ada@example.com"
    at = view.text.index("Ada")
    assert opencode[view.original(at) : view.original(at) + 3] == "Ada"


def test_the_yaml_forms_kubernetes_and_compose_files_use():
    text = (
        "labels: {team: payments-core, region: eu-west-1}\n"
        "password: &pw Sup3rS3cretPassw0rd\n"
        "owner: !!str Ada Byron\n"
        "quoted: 'it''s here'\n"
        'say: "say \\"hi\\" twice"\n'
        "note: first part\n"
        "  continued here\n"
    )
    found = _found(text, "/p/deploy.yaml")
    assert {
        ("labels.team", "payments-core"),
        ("labels.region", "eu-west-1"),
        ("password", "Sup3rS3cretPassw0rd"),
        ("owner", "Ada Byron"),
        ("quoted", "it''s here"),
        ("say", 'say \\"hi\\" twice'),
        ("note", "first part"),
        ("note", "continued here"),
    } <= found


# --- the second review's findings ----------------------------------------------------------------------------


def test_the_history_keeps_a_web_address_by_its_scheme_and_host_alone():
    """A web address can carry a person's email or a token in its path, query or fragment, and its user: the history
    keeps what a rule of `data_hosts` reads, the host, and a file's path whole."""
    from latet.gateway.recorder import kept_source

    url = "https://ada:hunter2@CRM.Example.com:8443/contacts/ada.byron@acme.it?token=abc123#notes"
    assert kept_source(url) == "https://crm.example.com/"
    assert kept_source("http://[::1]:8080/a?b=c") == "http://[::1]/"
    assert kept_source("https://example.com./x") == "https://example.com./"
    for path in ("/work/clients/acme/orders.json", "C:\\work\\acme\\orders.json", "file:///work/acme/a.json"):
        assert kept_source(path) == path
    assert kept_source("https://[broken/contacts?email=ada.byron@acme.it") is None  # no host: nothing a rule reads


def test_an_outputs_source_is_read_again_from_its_calls_input_as_the_request_carried_it():
    """Masking rewrites a call's input in place: read again then, the file or the address it names is the one the
    request carried, the form the history keeps."""
    read = {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/work/ada@acme.it/a.json"}}
    result = {"type": "tool_result", "tool_use_id": "r", "content": [{"type": "text", "text": "rows"}]}
    body = {"messages": [{"role": "assistant", "content": [read]}, {"role": "user", "content": [result]}]}
    [slot] = [s for s in anthropic.scan_request(body).slots if s.kind == SlotKind.TOOL_RESULT]
    read["input"]["file_path"] = "/work/zoe@fake.it/a.json"
    assert (slot.source, slot.sent_source()) == ("/work/ada@acme.it/a.json", "/work/zoe@fake.it/a.json")

    function = {"name": "read_file", "arguments": '{"path": "/work/ada@acme.it/rows.csv"}'}
    chat = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c", "type": "function", "function": function}],
            },
            {"role": "tool", "tool_call_id": "c", "content": "rows"},
        ]
    }
    [slot] = [s for s in openai_chat.scan_request(chat).slots if s.kind == SlotKind.TOOL_RESULT]
    function["arguments"] = '{"path": "/work/zoe@fake.it/rows.csv"}'
    assert (slot.source, slot.sent_source()) == ("/work/ada@acme.it/rows.csv", "/work/zoe@fake.it/rows.csv")

    call = {"type": "function_call", "call_id": "f", "name": "web_fetch", "arguments": '{"url": "https://a.example/x"}'}
    responses = {"input": [call, {"type": "function_call_output", "call_id": "f", "output": "page"}]}
    [slot] = [s for s in openai_responses.scan_request(responses).slots if s.kind == SlotKind.TOOL_RESULT]
    call["arguments"] = '{"url": "https://b.example/y"}'
    assert (slot.source, slot.sent_source()) == ("https://a.example/x", "https://b.example/y")


def test_the_history_keeps_a_source_with_where_its_values_are_masked():
    from latet.gateway.recorder import restored_source

    vault = Vault(ROOT_KEY)
    entity = vault.intern("EMAIL", EntityClass.PII, "ada.byron@acme.it")
    masked = f"/work/clients/{entity.surrogate}/orders.json"
    at = masked.index(entity.surrogate)
    found = [{"masked_start": at, "masked_end": at + len(entity.surrogate), "type": "EMAIL", "entity": entity.id}]
    record = BlockRecord("b", "$", "tool_result", False, True, tool="Read", source=masked, source_found=found)
    kept = ref_parts(block_ref(record))[5]
    assert "ada.byron@acme.it" not in json.dumps(kept)
    assert restored_source(kept, vault) == "/work/clients/ada.byron@acme.it/orders.json"
    vault.forget(entity.id)  # a value forgotten from the vault is gone from where the output came from too
    assert restored_source(kept, vault) == masked
    assert restored_source("/work/acme/a.json", vault) == "/work/acme/a.json"  # nothing masked in it, or kept before
    assert restored_source(None, vault) is None


def test_the_plugin_takes_no_path_longer_than_a_request_can_name():
    """The proxy takes no file or address longer than ``_SOURCE_LONGEST`` characters as where an output came from: nor
    does the Claude Code plugin's bridge."""
    from latet.bridge.api import Bridge
    from latet.protocols.base import _SOURCE_LONGEST

    def sent(path: str) -> str:
        mediator, _ = _mediator(data_files=["*.json"])
        item = {"text": _numbered(CUSTOMER), "kind": "tool_result", "tool": "Read", "path": path}
        _, out, _, _, _ = Bridge(mediator, "token")._mask({"session": "a", "items": [item]})
        return out[0]["text"]

    assert "platinum" not in sent("/work/acme/customer.json")
    longest = "/work/" + "d/" * ((_SOURCE_LONGEST - len("/work/customer.json")) // 2) + "customer.json"
    assert len(longest) <= _SOURCE_LONGEST and "platinum" not in sent(longest)
    too_long = "/work/" + "d/" * (_SOURCE_LONGEST // 2) + "customer.json"
    assert len(too_long) > _SOURCE_LONGEST and "platinum" in sent(too_long)


def _file_named_before(path: str, patterns: list[str]) -> bool:
    """``file_named`` as the first review left it: every suffix of the path joined again for each rule."""
    import fnmatch

    parts = [part for part in path.replace("\\", "/").lower().split("/") if part]
    if not parts:
        return False
    markdown = parts[-1].endswith((".md", ".markdown", ".mdx"))
    for pattern in patterns:
        rule = pattern.replace("\\", "/").lower().strip().strip("/")
        if not rule or (markdown and not rule.endswith((".md", ".markdown", ".mdx"))):
            continue
        if "/" not in rule:
            if fnmatch.fnmatchcase(parts[-1], rule):
                return True
        elif any(fnmatch.fnmatchcase("/".join(parts[i:]), rule) for i in range(len(parts))):
            return True
    return False


def test_a_rule_names_the_files_it_named_before():
    import random

    from latet.engine.detectors.fields import file_named

    rng = random.Random(7)
    segments = ["work", "acme", "exports", "q1", "a", "b", "data.json", "orders.csv", ".env.local", "notes.md", "x?y"]
    rules = [
        "*.json", "exports/**", "**/*.csv", "a/*", "b/orders.csv", "*/b/*", "?/b", "[ab]/orders.csv", "exports/*/*.csv",
        "*.md", "docs/*.md", "acme/exports", "/exports/q1/", "work/**/b", "[!a]/data.json", "x[?]y", "*a*/*b*", "q1",
        "EXPORTS/**", "a\\b", "*",
    ]  # fmt: skip
    for _ in range(3000):
        path = "/" + "/".join(rng.choice(segments) for _ in range(rng.randint(0, 6)))
        chosen = rng.sample(rules, rng.randint(1, 3))
        assert file_named(path, chosen) == _file_named_before(path, chosen), (path, chosen)


def test_a_deep_path_is_matched_against_many_rules_in_time_in_proportion_to_it():
    import time

    from latet.engine.detectors.fields import file_named

    path = "/" + "ab/" * 1360 + "x.csv"  # four thousand characters, the longest a source can be
    rules = [f"ab/*q{n}" for n in range(200)]  # as many rules as a repository's latet.yaml may set
    started = time.perf_counter()
    assert not file_named(path, rules)
    assert file_named(path, [*rules, "ab/ab/*.csv"])
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("tool", "path", "settings", "asked"),
    [
        ("WebFetch", "https://api.internal.example.com/o", {"data_hosts": ["api.internal.example.com"]}, "host_named"),
        ("read_file", "/work/acme/exports/orders.json", {"data_files": ["*.json"]}, "file_named"),
    ],
)
@pytest.mark.parametrize("data", ["strict", "normal"])
def test_a_documents_source_is_asked_once_not_for_each_of_its_values(monkeypatch, tool, path, settings, asked, data):
    """A JSON document is masked value by value: whether strict data mode covers it is asked of its source once, not
    for each value, which up to two hundred rules from a repository's file multiplied."""
    from latet.engine.detectors import fields

    calls: list[str] = []
    real = getattr(fields, asked)
    monkeypatch.setattr(fields, asked, lambda source, patterns: calls.append(source) or real(source, patterns))
    mediator, session = _mediator(data=data, **settings)
    rows = [
        {"customer": f"Customer {n} Smith", "status": f"STATE-{n:03}", "note": f"note number {n}"} for n in range(200)
    ]
    out = mediator.mask_value(
        json.dumps({"orders": rows}), SlotKind.TOOL_RESULT, session, MaskReport(), tool=tool, path=path
    )
    assert "STATE-007" not in out.text and "Customer 7 Smith" not in out.text
    assert len(calls) <= 2, len(calls)


EDITED = "The file {path} has been updated. Here's the result of running `cat -n` on a snippet of the edited file:\n"


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
@pytest.mark.parametrize("config", [{}, {"data": "strict"}, {"data": "strict", "data_tools": ["mcp__crm__*"]}])
def test_an_edit_of_a_file_a_rule_names_is_read_as_the_file(tool, config):
    """Claude Code's Edit, MultiEdit and Write echo numbered lines of the file they changed, lines the agent may never
    have read: a file a rule names is read there as a read of it is, whatever names the tools that count as data."""
    path = "/work/acme/exports/orders.csv"
    rows = "ORD-1005,80.00,Grace Hopper\nORD-1006,430.00,Alan Turing"
    text = EDITED.format(path=path) + _numbered_from(rows, 5)
    mediator, session = _mediator(data_files=["*.csv"], **config)
    out = mediator.mask_value(text, SlotKind.TOOL_RESULT, session, MaskReport(), tool=tool, path=path).text
    for value in ("ORD-1005", "80.00", "Grace Hopper", "ORD-1006", "430.00", "Alan Turing"):
        assert value not in out, value
    lines = out.split("\n")
    assert lines[0] == EDITED.format(path=path).rstrip("\n")
    assert [line.split("\t")[0] for line in lines[1:]] == ["     5", "     6"]  # its numbers stay
    assert [line.count(",") for line in lines[1:]] == [2, 2]  # and its cells
    assert strict_covers(tool, config.get("data_tools", ()), path, files=["*.csv"])


def test_an_edit_of_a_file_no_rule_names_leaves_as_today():
    path = "/work/acme/notes/orders.csv"
    text = EDITED.format(path=path) + _numbered_from("ORD-1005,80.00,Grace Hopper", 5)
    mediator, session = _mediator(data_files=["exports/**"])
    out = mediator.mask_value(text, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Edit", path=path).text
    assert "ORD-1005" in out
    assert not strict_covers("Edit", ["mcp__crm__*"], path, files=["exports/**"])
    assert strict_covers("Edit", (), path, files=["exports/**"])  # strict data mode covers every tool's output


def test_a_rule_for_every_subdomain_names_the_domain_too_and_a_final_dot_names_the_same_host():
    from latet.engine.detectors.fields import host_named

    assert host_named("https://salesforce.com/x", ["*.salesforce.com"])
    assert host_named("https://SALESFORCE.COM./x", ["*.salesforce.com"])
    assert host_named("https://eu1.salesforce.com./x", ["*.salesforce.com"])
    assert host_named("https://example.com./", ["example.com"])
    assert host_named("https://example.com/", ["example.com."])
    assert host_named("https://a.b.example.com/", ["*.*.example.com"])
    assert not host_named("https://notsalesforce.com/", ["*.salesforce.com"])
    assert not host_named("https://salesforce.com.evil.example/", ["*.salesforce.com"])
    assert not host_named("https://example.org./", ["example.com"])
    assert strict_covers("WebFetch", (), "https://salesforce.com/", hosts=["*.salesforce.com"])
