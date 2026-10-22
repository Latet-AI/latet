"""scripts/gen_reference.py: an option a help text names is code on the page, so Markdown leaves its hyphens alone."""

import importlib.util
import re
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "gen_reference", Path(__file__).resolve().parents[2] / "scripts" / "gen_reference.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


def test_an_option_in_a_sentence_becomes_code():
    assert gen._prose("Save the key, or forget it (--remove).") == "Save the key, or forget it (`--remove`)."


def test_code_stays_as_it_is():
    text = "The options choose otherwise, such as `--agents claude --no-autostart`."
    assert gen._prose(text) == text


def test_a_dash_between_words_is_not_an_option():
    assert gen._prose("one -- two, a well-known value") == "one -- two, a well-known value"


def test_a_type_with_a_check_of_its_own_is_shown_as_its_type():
    """An allow pattern is a text that RE2 compiles: the page says "list of text", never the check's repr (which holds
    an address in memory, and so changes at every run)."""
    from typing import Annotated

    from pydantic import AfterValidator

    checked = Annotated[str, AfterValidator(str.strip)]
    assert gen._type(checked) == "text" and gen._type(list[checked]) == "list of text"
    assert "Annotated" not in gen.configuration_page() and " at 0x" not in gen.configuration_page()


def test_the_cli_page_names_no_option_outside_code():
    outside_code = re.sub(r"`[^`]*`", "", gen.cli_page())
    assert re.findall(r"(?<![\w`-])--[a-z][\w-]*", outside_code) == []


def test_the_log_command_and_its_resumption_options_have_a_reference_section():
    page = gen.cli_page()
    section = re.search(r"^## `latet server log`\n(.*?)(?=^## |\Z)", page, re.MULTILINE | re.DOTALL)
    assert section is not None
    for option in ("--cursor", "--since", "--until", "--actor", "--action", "--limit", "--json"):
        assert f"`{option}`" in section[1]
    assert page.count("## `latet server log verify`") == 1


def test_a_group_with_options_of_its_own_has_a_reference_section():
    """`latet server --server/--fingerprint/--admin-token-file` and `latet server config|rules --key` come before the
    command's name: each group that takes options has a section of its own, once, with its options table, and is in
    the summary table."""
    page = gen.cli_page()
    groups = {
        "latet server": ("--server", "--fingerprint", "--admin-token-file"),
        "latet server config": ("--key",),
        "latet server rules": ("--key",),
    }
    for group, options in groups.items():
        assert page.count(f"## `{group}`\n") == 1, group
        section = re.search(rf"^## `{re.escape(group)}`\n(.*?)(?=^## |\Z)", page, re.MULTILINE | re.DOTALL)
        assert section is not None
        assert "| Option | Default | Description |" in section[1]
        for option in options:
            assert f"`{option}`" in section[1], (group, option)
        assert f"| `{group}` |" in page


def test_every_group_option_is_on_the_cli_page():
    """No option of a group is left out: a group's own options (those of its callback) each have a row somewhere."""
    import typer.main

    def walk(group, prefix):
        for name, command in group.commands.items():
            if command.hidden:
                continue
            if hasattr(command, "commands"):
                yield f"{prefix} {name}", command
                yield from walk(command, f"{prefix} {name}")

    page = gen.cli_page()
    found = []
    for name, group in walk(typer.main.get_command(gen.app), "latet"):
        for param in group.params:
            if param.param_type_name == "option" and not param.hidden and param.name != "help":
                found.append(name)
                section = re.search(rf"^## `{re.escape(name)}`\n(.*?)(?=^## |\Z)", page, re.MULTILINE | re.DOTALL)
                assert section is not None, name
                assert f"`{param.opts[0]}`" in section[1], (name, param.opts[0])
    assert "latet server" in found and "latet server log" in found


def test_the_configuration_reference_lists_every_server_setting_once():
    """A server operator can find every supported key, separate from computer settings."""
    from latet.server.settings import ServerSettings

    page = gen.configuration_page()
    section = re.search(r"^## Server settings\n(.*?)(?=^## |\Z)", page, re.MULTILINE | re.DOTALL)
    assert section is not None
    names = re.findall(r"^\| `([^`]+)` \|", section[1], re.MULTILINE)
    assert names == list(ServerSettings.model_fields)
    assert "| `port` | number | `5283` |" in section[1]
    assert "| `database_private_network` | true or false | `false` |" in section[1]
    assert "server.yaml" in section[1] and "LATET_SERVER_CONFIG" in section[1]


def test_the_server_reference_uses_model_descriptions(monkeypatch):
    """Changing a setting's source description changes the generated reference too."""
    from latet.server.settings import ServerSettings

    description = "Read budget in seconds; use `ocr_wait_seconds` for the queue | not the reader."
    monkeypatch.setattr(ServerSettings.model_fields["ocr_read_seconds"], "description", description)
    page = gen.configuration_page()
    row = next(line for line in page.splitlines() if line.startswith("| `ocr_read_seconds` |"))
    assert "Read budget in seconds; use `ocr_wait_seconds` for the queue \\| not the reader." in row
    assert "| `ocr_read_seconds` |" not in page.split("## Server settings\n", 1)[0]


def test_the_connection_environment_rows_have_distinct_documented_roles():
    page = gen.configuration_page()
    section = page.split("## Environment variables\n", 1)[1].split("\n## ", 1)[0]
    rows = dict(re.findall(r"^\| `([^`]+)` \| (.+) \|$", section, re.MULTILINE))
    for variable in ("LATET_CONNECTION", "LATET_SERVER_URL", "LATET_SERVER_FINGERPRINT", "LATET_SERVER_ADMIN_TOKEN"):
        assert variable in rows
        assert section.count(f"| `{variable}` |") == 1
    assert "connection" in rows["LATET_CONNECTION"].lower()
    assert "fingerprint" in rows["LATET_SERVER_FINGERPRINT"].lower()
    assert "administration" in rows["LATET_SERVER_ADMIN_TOKEN"].lower()
    assert "secret" in rows["LATET_SERVER_ADMIN_TOKEN"].lower()
