"""A version can publish only after its exact main commit passed the complete CI workflow."""

import copy
import importlib.util
import io
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
OTHER = "b" * 40
REPOSITORY = "Latet-AI/latet"
PREFIX = f"/repos/{REPOSITORY}"


def gate_module():
    path = ROOT / "scripts/check_release_ci.py"
    assert path.is_file(), "the release has no executable fail-closed CI gate"
    spec = importlib.util.spec_from_file_location("check_release_ci", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_record(**changes):
    return {
        "id": 100,
        "workflow_id": 17,
        "path": ".github/workflows/ci.yml",
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_number": 20,
        "run_attempt": 1,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        **changes,
    }


class Api:
    def __init__(self):
        self.tag = {"ref": "refs/tags/v0.10.3", "object": {"type": "commit", "sha": SHA}}
        self.tags = {}
        self.workflow = {"id": 17, "path": ".github/workflows/ci.yml", "state": "active"}
        self.comparison = {"status": "ahead", "merge_base_commit": {"sha": SHA}}
        self.runs = [run_record()]
        self.details = {100: run_record()}
        self.calls = []
        self.total = None
        self.fail = None

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        if self.fail and self.fail in path:
            raise OSError("test API unavailable")
        if path == f"{PREFIX}/git/ref/tags/v0.10.3":
            return copy.deepcopy(self.tag)
        if path.startswith(f"{PREFIX}/git/tags/"):
            return copy.deepcopy(self.tags[path.rsplit("/", 1)[-1]])
        if path == f"{PREFIX}/compare/{SHA}...main":
            return copy.deepcopy(self.comparison)
        if path == f"{PREFIX}/actions/workflows/ci.yml":
            return copy.deepcopy(self.workflow)
        if path == f"{PREFIX}/actions/workflows/17/runs":
            assert params is not None
            assert params == {
                "event": "push",
                "branch": "main",
                "head_sha": SHA,
                "per_page": 100,
                "page": params["page"],
            }
            start = (params["page"] - 1) * 100
            return {
                "total_count": len(self.runs) if self.total is None else self.total,
                "workflow_runs": copy.deepcopy(self.runs[start : start + 100]),
            }
        if path.startswith(f"{PREFIX}/actions/runs/"):
            return copy.deepcopy(self.details[int(path.rsplit("/", 1)[-1])])
        raise AssertionError(f"unexpected API request: {path}")


def check(api):
    return gate_module().check_release(api, REPOSITORY, "v0.10.3", SHA)


def test_release_requires_exact_successful_main_push():
    api = Api()
    assert check(api) == run_record()
    assert api.calls[-1][0] == f"{PREFIX}/actions/runs/100"


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": OTHER},
        {"head_branch": "feature"},
        {"event": "pull_request"},
        {"event": "workflow_dispatch"},
        {"workflow_id": 18},
        {"path": ".github/workflows/other.yml"},
        {"repository": {"full_name": "other/repo"}},
        {"head_repository": {"full_name": "other/repo"}},
        {"status": "queued"},
        {"status": "in_progress"},
        {"status": "waiting"},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"conclusion": "skipped"},
        {"conclusion": "neutral"},
        {"conclusion": "timed_out"},
        {"conclusion": None},
        {"run_attempt": 0},
    ],
)
def test_release_rejects_unrelated_or_incomplete_evidence(changes):
    api = Api()
    api.runs = [run_record(**changes)]
    api.details = {100: run_record(**changes)}
    with pytest.raises(RuntimeError):
        check(api)


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
def test_release_rechecks_latest_attempt_instead_of_accepting_old_success(status):
    api = Api()
    api.details[100] = run_record(run_attempt=2, status=status, conclusion="failure" if status == "completed" else None)
    with pytest.raises(RuntimeError):
        check(api)


def test_release_accepts_successful_latest_rerun():
    api = Api()
    api.details[100] = run_record(run_attempt=2)
    assert check(api)["run_attempt"] == 2


def test_release_does_not_fall_back_to_older_successful_run():
    api = Api()
    api.runs.append(run_record(id=101, run_number=21, status="queued", conclusion=None))
    api.details[101] = api.runs[-1]
    with pytest.raises(RuntimeError):
        check(api)


def test_release_reads_every_page_before_selecting_latest_run():
    api = Api()
    api.runs = [run_record(id=n, run_number=n) for n in range(1, 102)]
    api.details[101] = run_record(id=101, run_number=101, conclusion="failure")
    with pytest.raises(RuntimeError):
        check(api)
    assert any(params and params.get("page") == 2 for _, params in api.calls)


@pytest.mark.parametrize("total", [0, 2, 1000, 1001])
def test_release_fails_closed_on_absent_incomplete_or_truncated_inventory(total):
    api = Api()
    api.total = total
    if total == 0:
        api.runs = []
    with pytest.raises(RuntimeError):
        check(api)


def test_release_peels_annotated_tags_before_matching_ci():
    api = Api()
    api.tag["object"] = {"type": "tag", "sha": OTHER}
    api.tags[OTHER] = {"object": {"type": "commit", "sha": SHA}}
    assert check(api)["head_sha"] == SHA


@pytest.mark.parametrize("change", ["sha", "ref", "type", "cycle"])
def test_release_rejects_moved_malformed_or_cyclic_tag(change):
    api = Api()
    if change == "ref":
        api.tag["ref"] = "refs/tags/elsewhere"
    elif change == "cycle":
        api.tag["object"] = {"type": "tag", "sha": OTHER}
        api.tags[OTHER] = {"object": {"type": "tag", "sha": OTHER}}
    else:
        api.tag["object"][change] = OTHER if change == "sha" else "tree"
    with pytest.raises(RuntimeError):
        check(api)


@pytest.mark.parametrize(
    "comparison",
    [
        {"status": "behind", "merge_base_commit": {"sha": SHA}},
        {"status": "diverged", "merge_base_commit": {"sha": OTHER}},
        {"status": "ahead", "merge_base_commit": {"sha": OTHER}},
    ],
)
def test_release_requires_tag_commit_on_main(comparison):
    api = Api()
    api.comparison = comparison
    with pytest.raises(RuntimeError):
        check(api)


@pytest.mark.parametrize("endpoint", ["git/ref", "compare", "workflows/ci.yml", "workflows/17/runs", "actions/runs/"])
def test_release_propagates_api_or_authentication_failure(endpoint):
    api = Api()
    api.fail = endpoint
    with pytest.raises((RuntimeError, OSError)):
        check(api)


@pytest.mark.parametrize(
    "changes", [{"id": 101}, {"run_number": 21}, {"run_attempt": 0}, {"path": ".github/workflows/ci.yml@other"}]
)
def test_release_rejects_inconsistent_refreshed_run(changes):
    api = Api()
    api.details[100] = run_record(**changes)
    with pytest.raises(RuntimeError):
        check(api)


@pytest.mark.parametrize(
    "workflow",
    [
        {"id": 18, "path": ".github/workflows/elsewhere.yml", "state": "active"},
        {"id": 17, "path": ".github/workflows/ci.yml", "state": "disabled_manually"},
        {},
    ],
)
def test_release_requires_the_actual_active_workflow_file(workflow):
    api = Api()
    api.workflow = workflow
    with pytest.raises(RuntimeError):
        check(api)


def test_release_rejects_duplicate_inventory_entries():
    api = Api()
    api.runs *= 2
    with pytest.raises(RuntimeError):
        check(api)


@pytest.mark.parametrize(
    ("repository", "tag", "commit"),
    [
        ("Latet-AI/latet;echo", "v0.10.3", SHA),
        (REPOSITORY, "v1\noutput=evil", SHA),
        (REPOSITORY, "v$(echo)", SHA),
        (REPOSITORY, "v0.10.3", "main"),
    ],
)
def test_release_validates_inputs_before_api_requests(repository, tag, commit):
    api = Api()
    with pytest.raises(RuntimeError):
        gate_module().check_release(api, repository, tag, commit)
    assert api.calls == []


@pytest.mark.parametrize(
    ("status", "body"), [(401, b"{}"), (403, b"{}"), (500, b"{}"), (200, b"not json"), (200, b"[]")]
)
def test_api_rejects_authentication_errors_and_malformed_responses(monkeypatch, status, body):
    module = gate_module()
    api = module.GitHubApi("test-read-token")

    class Response(io.BytesIO):
        status: int

    def opened(request, timeout):
        assert request.full_url == "https://api.github.com/repos/Latet-AI/latet/actions/workflows/ci.yml"
        assert request.get_header("Authorization") == "Bearer test-read-token"
        assert timeout == 10
        response = Response(body)
        response.status = status
        return response

    monkeypatch.setattr(api.opener, "open", opened)
    with pytest.raises(RuntimeError):
        api(f"{PREFIX}/actions/workflows/ci.yml")
    assert module.NoRedirects().redirect_request(None, None, 302, None, None, "https://elsewhere.example") is None


@pytest.mark.parametrize("success", [False, True])
def test_entrypoint_writes_only_verified_commit_and_safe_summary(monkeypatch, tmp_path, capsys, success):
    module, api = gate_module(), Api()
    if not success:
        api.details[100]["conclusion"] = "failure"
    output, summary = tmp_path / "output", tmp_path / "summary"
    for name, value in {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_SHA": SHA,
        "GITHUB_REF_NAME": "v0.10.3",
        "GH_TOKEN": "test-read-token",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "GitHubApi", lambda token: api)
    assert module.main() == (0 if success else 1)
    captured = capsys.readouterr()
    assert "test-read-token" not in captured.out + captured.err
    if success:
        assert output.read_text(encoding="utf-8") == f"commit={SHA}\n"
        assert "run 100, attempt 1" in summary.read_text(encoding="utf-8")
        assert "completed / success" in summary.read_text(encoding="utf-8")
    else:
        assert not output.exists()
        assert not summary.exists()


def test_every_release_job_depends_on_ci_and_checkouts_use_verified_commit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "ci" in jobs, "version publication has no main CI gate"
    assert jobs["ci"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["ci"]["timeout-minutes"] <= 5
    assert "commit" in jobs["ci"]["outputs"]
    gate_steps = jobs["ci"]["steps"]
    gate_step = next(step for step in gate_steps if "check_release_ci.py" in step.get("run", ""))
    assert gate_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "${{" not in gate_step["run"]
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in jobs.items():
        if name == "ci":
            continue
        needs = job.get("needs", [])
        assert "ci" in (needs if isinstance(needs, list) else [needs]), name
        assert "always()" not in job.get("if", "")
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ needs.ci.outputs.commit }}"
                assert step["with"]["persist-credentials"] is False
