"""The licenses travel with the code: LICENSE puts every `ee` directory under the Latet Enterprise License, whose one
text is ee/LICENSE; LICENSING.md says that the Community Edition is free for every company, that the Enterprise
features need a license file checked offline, how users are counted and how to ask for the trial; the package names
both licenses and ships the Enterprise one."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_ee_folders_are_under_the_enterprise_license_whose_text_is_one():
    preamble = (ROOT / "LICENSE").read_text("utf-8")
    assert "Files in a directory named `ee`" in preamble and "`ee/LICENSE`" in preamble
    assert (ROOT / "ee" / "LICENSE").read_text("utf-8").startswith("# The Latet Enterprise License")
    assert not (ROOT / "NOTICE").exists()
    for folder in (ROOT / "src" / "latet" / "ee", ROOT / "dashboard" / "src" / "ee"):
        assert not (folder / "LICENSE").exists(), folder  # LICENSE's preamble covers every `ee` directory


def test_licensing_says_who_needs_enterprise_the_license_file_and_the_trial():
    text = (ROOT / "LICENSING.md").read_text("utf-8")
    assert "free for everyone, companies of any size included" in text and "OEM" in text
    assert "## The Enterprise Edition" in text and "license file" in text and "API key" in text
    assert "32 days, once per company" in text and "no grace" in text and "No calls home." in text
    assert "250" not in text and "128 days" not in text and "NOTICE" not in text.replace("THIRD_PARTY_NOTICES", "")


def test_the_package_names_both_licenses_and_ships_the_enterprise_one():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["license"] == "LicenseRef-Latet-Sustainable-Use-1.0 AND LicenseRef-Latet-Enterprise-1.0"
    assert project["license-files"] == ["LICENSE", "LICENSING.md", "ee/LICENSE"]
    assert "COPY ee/LICENSE ee/LICENSE" in (ROOT / "Dockerfile").read_text("utf-8")
