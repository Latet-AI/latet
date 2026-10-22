"""What the server's extra asks of its two drivers: the versions the code was written against and tested with, since it
leans on what they do (the bus on how redis-py reads a blocking subscription and bounds a handshake, the database on
how psycopg and libpq take an address)."""

import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]
# The oldest version of each that the server is written for, and the lowest the extra may therefore allow.
FLOORS = {"redis": Version("8.1"), "psycopg": Version("3.3")}


def _server_extra() -> dict[str, Requirement]:
    found = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return {
        requirement.name: requirement
        for requirement in map(Requirement, found["project"]["optional-dependencies"]["server"])
    }


def _lowest(requirement: Requirement) -> Version | None:
    """The version that a requirement is no older than: the bound of its ``>=`` (none if it has no such bound)."""
    bounds = [Version(one.version) for one in requirement.specifier if one.operator in (">=", "==", "~=")]
    return max(bounds, default=None)


def test_the_server_extra_asks_for_the_versions_of_the_drivers_the_code_rests_on():
    asked = _server_extra()
    for name, floor in FLOORS.items():
        assert name in asked, f"the server extra does not ask for {name}"
        lowest = _lowest(asked[name])
        assert lowest is not None and lowest >= floor, f"{name}: {asked[name]} allows versions older than {floor}"


def test_the_versions_the_tests_run_with_are_ones_the_extra_allows():
    asked = _server_extra()
    for name in FLOORS:
        assert asked[name].specifier.contains(version(name)), f"{name} {version(name)} is not {asked[name]}"
