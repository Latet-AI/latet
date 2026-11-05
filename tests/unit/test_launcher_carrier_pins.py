"""The pinned launch-key carriers of OpenCode and Gemini CLI (P2-8a; root's rulings on its findings 1, 2 and 7,
2026-11-04).

- OpenCode carries the key through `options.headers` naming `{env:...}` in `OPENCODE_CONFIG_CONTENT`, substituted from
  1.19.5, the version whose source was read (OpenCode 810c0eba packages/opencode/src/config/config.ts:227-238,
  482-489; config/variable.ts:34-38). OpenCode 1.0.0 parses that variable with no substitution (config.ts:44-46) and
  would send the reference itself, which Latet refuses: `latet opencode` passes a key only from 1.19.5, and none when
  it cannot read the version.
- Gemini CLI carries it through `GEMINI_CLI_CUSTOM_HEADERS` from 0.19.0 to 0.66.0 (Gemini CLI 97041320
  packages/core/src/core/contentGenerator.ts:240-241, 275-283; utils/customHeaderUtils.ts:10-41), and 0.67.0-preview's
  newer split keeps the same entries. In ACP mode (`--acp`, `--experimental-acp`: packages/cli/src/config/config.ts:
  363-371) the editor's `authenticate` call can set another base URL and headers
  (packages/cli/src/acp/acpRpcDispatcher.ts:128-157): no key then. `latet gemini` reads no version: an older Gemini CLI
  drops the key, its session stays claimed and `latet doctor` names the version.

Each such session is claimed (D7: tighten only), the launcher says why in a sentence, and `latet doctor` names the
versions below each floor, all from one table (`latet.launch_carriers`). No OpenCode, Gemini CLI or provider is run."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from latet import wrappers
from latet.cli.main import app
from latet.launch_session import keyless_words

from .test_gemini_wrapper import gemini_home  # noqa: F401 - fixture
from .test_launcher_carriers import BASE, HEADER, KEY, _remaining_carrier_invocation
from .test_wrapper_routes import launched  # noqa: F401 - fixture


def _said(monkeypatch):
    """The reasons the launchers gave for starting an agent without a key, as `_say_keyless` was asked."""
    from latet.cli import main as cli_main

    said: list[tuple[str, str | None]] = []
    original = cli_main._say_keyless

    def say(agent, reason):
        said.append((agent, reason))
        original(agent, reason)

    monkeypatch.setattr(cli_main, "_say_keyless", say)
    return said


# --- OpenCode: a key from its verified version alone (ruling 1) ------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "reason"),
    [((1, 19, 4), "older-opencode"), ((1, 0, 0), "older-opencode"), (None, "unknown-opencode")],
)
def test_opencode_before_its_verified_carrier_passes_no_key(launched, gemini_home, monkeypatch, version, reason):  # noqa: F811
    monkeypatch.setattr(wrappers, "opencode_version", lambda exe="opencode": version, raising=False)
    said = _said(monkeypatch)
    _remaining_carrier_invocation(launched, gemini_home, monkeypatch, "opencode", blocked="older")
    assert said == [("opencode", reason)]


@pytest.mark.parametrize("version", [(1, 19, 5), (1, 20, 0), (2, 0, 0)])
def test_opencode_from_its_verified_carrier_passes_its_key(launched, gemini_home, monkeypatch, version):  # noqa: F811
    monkeypatch.setattr(wrappers, "opencode_version", lambda exe="opencode": version, raising=False)
    _remaining_carrier_invocation(launched, gemini_home, monkeypatch, "opencode")


def test_opencode_version_reads_its_release_number(monkeypatch):
    """`opencode --version` prints its release number; anything else is no version (the launcher passes no key)."""
    outputs = iter(["1.19.5\n", "opencode 1.20.3 (abc)\n", "unknown\n"])
    monkeypatch.setattr(wrappers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        wrappers.subprocess, "run", lambda *a, **k: type("Done", (), {"stdout": next(outputs), "stderr": ""})()
    )
    assert wrappers.opencode_version() == (1, 19, 5)
    assert wrappers.opencode_version() == (1, 20, 3)
    assert wrappers.opencode_version() is None
    monkeypatch.setattr(wrappers.shutil, "which", lambda name: None)
    assert wrappers.opencode_version() is None


# --- Gemini CLI: no key in ACP mode (ruling 2) -----------------------------------------------------------------------


def _chosen(home):
    (home / ".gemini" / "settings.json").write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}', "utf-8")


@pytest.mark.parametrize(
    "args",
    [
        ["--acp"],
        ["--experimental-acp"],
        ["--acp=true"],
        ["--experimentalAcp"],
        ["-m", "gemini-2.5-pro", "--experimental-acp"],
    ],
)
def test_gemini_in_acp_mode_is_given_no_key(gemini_home, args):  # noqa: F811
    _chosen(gemini_home)
    assert wrappers.gemini_carrier_verified("gemini-api-key", [], BASE)
    assert not wrappers.gemini_carrier_verified("gemini-api-key", args, BASE)


@pytest.mark.parametrize("args", [["--acp-like-name"], ["--approval-mode", "yolo"], ["-p", "hello"]])
def test_other_arguments_keep_the_gemini_carrier(gemini_home, args):  # noqa: F811
    """An option that only starts like ACP's is not ACP's: the carrier stays."""
    _chosen(gemini_home)
    assert wrappers.gemini_carrier_verified("gemini-api-key", args, BASE)


def test_latet_gemini_in_acp_mode_passes_no_key_and_says_why(launched, gemini_home, monkeypatch):  # noqa: F811
    _chosen(gemini_home)
    runs, _, _ = launched
    monkeypatch.setattr(wrappers, "_launch_call", lambda *a, **k: pytest.fail("a launch key was opened"))
    said = _said(monkeypatch)
    result = CliRunner().invoke(app, ["gemini", "--acp"])
    assert result.exit_code == 0, result.output
    assert said == [("gemini", "gemini-acp")]
    assert "ACP" in result.output and "loosenings do not apply" in result.output
    [run] = runs
    assert KEY not in json.dumps(run["env"]) and HEADER not in run["env"].get("GEMINI_CLI_CUSTOM_HEADERS", "").lower()


# Gemini CLI 0.67.0-preview.0 (578f0332, #29865) splits GEMINI_CLI_CUSTOM_HEADERS at a comma followed by a header
# name of token characters and a colon; 0.19.0-0.66.0 at a comma followed by anything but a comma or colon, then a
# colon (customHeaderUtils.ts:10-41). Both are emulated here with JavaScript's \s and \w (ASCII) as Python has them.
_JS_SPACE = re.escape(wrappers._JS_SPACE)
_SPLIT_066 = re.compile(",(?=[" + _JS_SPACE + "]*[^,:]+:)")
_SPLIT_067 = re.compile(",(?=[" + _JS_SPACE + "]*[A-Za-z0-9_!#$%&'*+.^`|~-]+[" + _JS_SPACE + "]*:)")


def _parsed(value: str, split: re.Pattern[str]) -> list[tuple[str, str]]:
    """customHeaderUtils' parse: split, trim, skip an entry with no name, the name and value either side of its first
    colon. Every entry, in order (a Headers object then keeps the last of a name)."""
    out = []
    for entry in split.split(value):
        name, separator, rest = entry.strip(wrappers._JS_SPACE).partition(":")
        name = name.strip(wrappers._JS_SPACE)
        if separator and name:
            out.append((name, rest.strip(wrappers._JS_SPACE)))
    return out


def test_the_two_splits_differ_where_the_pin_says():
    """The emulation is not vacuous: one comma 0.66 splits at and 0.67-preview does not."""
    odd = "X-Odd: (a, b): c"
    assert len(_parsed(odd, _SPLIT_066)) == 2 and len(_parsed(odd, _SPLIT_067)) == 1


@pytest.mark.parametrize(
    "original",
    [
        "",
        "X-One: a",
        "X-One: a,b, X-Url: https://host:443, X-One: last",
        f" X-Two: 1, \N{ZERO WIDTH NO-BREAK SPACE}X-Latet-Launch-Key: {KEY[:-1]}B, X-Three: c:d",
        "X-Odd: (a, b): c, X-Next: n",  # a comma the older split takes and the newer one does not
        "X-Spaced : v, x-latet-launch-key : old",
    ],
)
def test_the_key_entry_parses_the_same_under_both_header_splits(original):
    """Ruling 2: the entries `gemini_headers` keeps are the user's under 0.66's split and under 0.67-preview's, and the
    key it appends is one entry of its own under both; no surviving entry carries the reserved name."""
    sent = wrappers.gemini_headers(original, KEY)
    for split in (_SPLIT_066, _SPLIT_067):
        parsed = _parsed(sent, split)
        assert [value for name, value in parsed if name.lower() == HEADER] == [KEY], (split.pattern, parsed)
        user = [(n, v) for n, v in _parsed(original, split) if n.lower() != HEADER]
        assert [(n, v) for n, v in parsed if n.lower() != HEADER] == user


# --- the sentences, and the pins in one table (ruling 7) -------------------------------------------------------------


def _sentence(agent: str, reason: str) -> str:
    words = keyless_words(agent, reason)
    assert words is not None, (agent, reason)
    return words[0]


def test_the_pinned_carriers_say_why_in_a_sentence():
    assert "1.19.5" in _sentence("opencode", "older-opencode")
    assert "version" in _sentence("opencode", "unknown-opencode")
    assert "ACP" in _sentence("gemini", "gemini-acp")
    assert "0.19" in _sentence("gemini", "older-gemini")


def test_one_table_pins_each_launchers_carrier():
    from latet.launch_carriers import CARRIERS, floor

    pinned = {carrier.agent: carrier for carrier in CARRIERS}
    assert set(pinned) == {"claude-code", "codex", "opencode", "gemini-cli", "copilot"}
    assert floor("opencode") == (1, 19, 5) and "1.19.5" in pinned["opencode"].verified
    assert floor("gemini-cli") == (0, 19, 0) and {"0.19.0", "0.66.0"} <= set(pinned["gemini-cli"].verified)
    assert floor("codex") == (0, 115, 0) and "0.162.0" in pinned["codex"].verified
    assert floor("claude-code") is None and floor("copilot") is None
    assert floor("opencode") == wrappers.OPENCODE_CARRIER
    for carrier in CARRIERS:
        assert carrier.anchors and all(anchor.strip() for anchor in carrier.anchors)
        assert carrier.command.startswith("latet ")


@pytest.mark.parametrize(
    ("agent", "version", "upstream", "reason"),
    [
        ("codex", "0.114.0", "custom", "older-codex"),
        ("codex", "0.115.0", "custom", None),
        ("codex", "0.162.0", "chatgpt", "builtin-provider"),
        ("opencode", "1.19.4", "anthropic", "older-opencode"),
        ("opencode", "1.19.5", "anthropic", None),
        ("opencode", None, "anthropic", None),
        ("gemini-cli", "0.18.0", "gemini", "older-gemini"),
        ("gemini-cli", "0.19.0", "gemini", None),
        ("gemini-cli", "0.67.0-preview.0", "gemini", None),
        ("claude-code", "2.1.312", "anthropic", None),
    ],
)
def test_doctor_names_a_version_below_each_floor_in_the_launchers_words(agent, version, upstream, reason):
    """`latet doctor` reads the same table and says the launcher's own sentence: one source for both."""
    from latet.doctor import upgrade
    from latet.launch_session import _WHY

    assert upgrade._no_carrier(agent, version, upstream) == (None if reason is None else _WHY[reason])
