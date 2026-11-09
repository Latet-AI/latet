"""Every place that says Latet's version says the same one: the release checks the package's and the plugin's against
the tag (release.yml), the dashboard's mock shows it in every screenshot of the documentation, and the newest release
in the repository's CHANGELOG.md is it, so a version is never published with that file a release behind."""

import json
import re
import tomllib
from pathlib import Path

from latet import __version__

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text("utf-8")


def test_every_place_says_the_same_version():
    package = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
    plugin = json.loads(_read("src/latet/claude_plugin/.claude-plugin/plugin.json"))["version"]
    market = json.loads(_read(".claude-plugin/marketplace.json"))["plugins"][0]["version"]
    hook = re.search(r"^const PLUGIN_VERSION = '(.*)'$", _read("src/latet/claude_plugin/hooks/register.ts"), re.M)
    mock = re.findall(r"version: '([^']*)'", _read("dashboard/src/mock/index.ts"))
    newest = re.search(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", _read("CHANGELOG.md"), re.M)
    assert hook is not None and mock and newest
    assert {package, __version__, plugin, market, hook.group(1), *mock, newest.group(1)} == {package}
