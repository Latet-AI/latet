"""The documentation of each version travels with it: the wheel and the container image carry the bundle the daemon
serves at /_latet/docs/, checked before it ships (scripts/check_docs_bundle.py writes the hashes the daemon's policy
lists), and the release fails when the wheel does not carry it. CI builds it on every change and checks the dashboard's
links against it."""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _active_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text("utf-8").splitlines() if line.strip() and not line.startswith("#")]


def _steps(workflow: str, job: str) -> list[dict]:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text("utf-8"))["jobs"]
    return jobs[job]["steps"]


def _runs(steps: list[dict]) -> str:
    return "\n".join(str(step.get("run", "")) for step in steps)


def test_the_wheel_ships_the_documentation_bundle():
    build = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["hatch"]["build"]
    assert "src/latet/docs/**" in build["artifacts"]
    assert "src/latet/docs/" in _active_lines(ROOT / ".gitignore")  # built, never committed


def test_the_image_builds_the_documentation_checks_it_and_ships_it():
    text = (ROOT / "Dockerfile").read_text("utf-8")
    docs_stage = text.split("AS docs", 1)[1].split("FROM ", 1)[0]
    assert "npm ci" in docs_stage and "npm run build:bundle" in docs_stage
    build_stage = text.split("AS build", 1)[1].split("FROM ", 1)[0]
    assert "COPY --from=docs /src/src/latet/docs/ src/latet/docs/" in build_stage
    check = build_stage.index("check_docs_bundle.py --root src/latet/docs")
    assert check < build_stage.index("uv build"), "the bundle is checked (and its .csp.json written) before the wheel"
    ignored = _active_lines(ROOT / ".dockerignore")
    assert "docs/" not in ignored and "src/latet/docs/" in ignored  # its sources go in, a local build does not
    assert "!scripts/check_docs_bundle.py" in ignored


def test_the_release_builds_the_documentation_and_the_wheel_carries_it():
    docs = _steps("release.yml", "docs")
    assert "npm run build:bundle" in _runs(docs)
    for script in ("check_docs_bundle.py", "gen_docs_anchors.py --check", "check_docs_anchors.py"):
        assert script in _runs(docs), script
    upload = next(step for step in docs if str(step.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["path"] == "src/latet/docs"
    assert upload["with"]["include-hidden-files"] is True  # .csp.json, without which the daemon serves nothing
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8"))["jobs"]
    assert "docs" in jobs["build"]["needs"]
    build = _steps("release.yml", "build")
    carries = next(step for step in build if step.get("name") == "The wheel carries the docs")
    assert "latet/docs/index.html" in carries["run"] and "latet/docs/.csp.json" in carries["run"]
    assert re.search(r"name: docs-bundle", (ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8"))


def test_ci_builds_the_documentation_and_checks_every_dashboard_link():
    runs = _runs(_steps("ci.yml", "docs-bundle"))
    assert "npm run build:bundle" in runs
    assert "check_docs_bundle.py" in runs
    assert "gen_docs_anchors.py --check --root src/latet/docs" in runs
    assert "check_docs_anchors.py --root src/latet/docs" in runs
