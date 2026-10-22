"""Every place that says Latet's version says the same one: the release checks the package's and the plugin's against
the tag (release.yml), and the dashboard's mock shows it in every screenshot of the documentation."""

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
    assert hook is not None and mock
    assert {package, __version__, plugin, market, hook.group(1), *mock} == {package}
