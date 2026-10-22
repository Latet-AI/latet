"""The CLAUDE.md files Claude Code loads, checked for values its auto mode reviewer must never get."""

from latet.config import Config, IdentityConfig, ProjectEntry, save_config
from latet.doctor.checks import check_instructions
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.instructions import describe, findings, instruction_files

from ..conftest import ROOT_KEY

PASSWORD = "S3cr3tPa55w0rd"
CLAUDE_MD = f"""# Shop

Run the tests with `npm test`.

## Databases
- staging: postgres://app:{PASSWORD}@db.internal:5432/shop
- the client to write to: anna.verdi@verdi-studio.it
"""


def _project(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    (home / "CLAUDE.md").write_text("# Me\nI like short answers.\n", "utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    project = tmp_path / "shop"
    (project / "src").mkdir(parents=True)
    (project / "CLAUDE.md").write_text(CLAUDE_MD, "utf-8")
    return project


def test_the_files_claude_code_loads_are_found_from_the_folder_up(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    files = instruction_files(project / "src")
    assert files[0] == tmp_path / "claude" / "CLAUDE.md"  # yours first
    assert project / "CLAUDE.md" in files  # the project's, found from its subfolder
    assert len(files) == len(set(files))


def test_what_never_goes_to_the_reviewer_is_named_by_line_and_type_only(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    masker = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY)).masker
    found = findings(instruction_files(project), masker)
    assert {(f.etype, f.line) for f in found} == {("URL_PASSWORD", 6), ("EMAIL", 7)}
    [line] = describe(found)
    assert "line 6" in line and "line 7" in line and ".env" in line
    assert PASSWORD not in line and "anna.verdi" not in line


def test_latet_doctor_warns_about_them(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    save_config(Config(identity=IdentityConfig(enabled=False)))
    [check] = check_instructions(project)
    assert check.status == "warn" and "auto mode's reviewer" in check.detail and PASSWORD not in check.detail
    (project / "CLAUDE.md").write_text("# Shop\nThe database URL is in DATABASE_URL, in .env.\n", "utf-8")
    [check] = check_instructions(project)
    assert check.status == "ok"


def test_a_placeholder_you_let_through_in_the_project_is_no_finding(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    personal = ProjectEntry(path=str(project), allow_values=[PASSWORD, "anna.verdi@verdi-studio.it"])
    save_config(Config(identity=IdentityConfig(enabled=False), projects=[personal]))
    [check] = check_instructions(project / "src")
    assert check.status == "ok"
    # A placeholder written in a form Latet knows is no finding either.
    (project / "CLAUDE.md").write_text("- staging: postgres://app:<password>@db.internal:5432/shop\n", "utf-8")
    save_config(Config(identity=IdentityConfig(enabled=False)))
    [check] = check_instructions(project)
    assert check.status == "ok"
