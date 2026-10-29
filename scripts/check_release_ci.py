"""Fail closed unless a version's exact main commit passed the complete push CI workflow.

Only GitHub read APIs and the standard library are used. No repository code is imported or executed.
The accepted result is a snapshot: a later rerun can change that run's state.
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

WORKFLOW = ".github/workflows/ci.yml"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def positive(value):
    return type(value) is int and value > 0


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def run_identity(run, workflow_id, repository, commit):
    require(
        run["workflow_id"] == workflow_id
        and run["path"] == WORKFLOW
        and run["head_sha"] == commit
        and run["head_branch"] == "main"
        and run["event"] == "push"
        and run["repository"]["full_name"] == repository
        and run["head_repository"]["full_name"] == repository
        and all(positive(run[key]) for key in ("id", "run_number", "run_attempt")),
        "CI evidence does not identify the exact workflow and main push",
    )


def check_release(api, repository, tag, expected_sha):
    """Validate real API responses. ``api(path, params=None)`` performs one authenticated GET."""
    require(
        isinstance(repository, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository),
        "Invalid repository identity",
    )
    require(isinstance(tag, str) and re.fullmatch(r"v[0-9][A-Za-z0-9.+-]*", tag), "Invalid version tag")
    require(sha(expected_sha), "Invalid expected commit")
    base = f"/repos/{repository}"
    try:
        reference = api(f"{base}/git/ref/tags/{tag}")
        require(reference["ref"] == f"refs/tags/{tag}", "Tag reference changed")
        target, seen = reference["object"], set()
        for _ in range(10):
            require(sha(target["sha"]), "Invalid tag object")
            if target["type"] == "commit":
                break
            require(target["type"] == "tag" and target["sha"] not in seen, "Invalid annotated tag chain")
            seen.add(target["sha"])
            target = api(f"{base}/git/tags/{target['sha']}")["object"]
        require(target["type"] == "commit" and target["sha"] == expected_sha, "Tag does not match the event commit")
        comparison = api(f"{base}/compare/{expected_sha}...main")
        require(
            comparison["status"] in {"ahead", "identical"} and comparison["merge_base_commit"]["sha"] == expected_sha,
            "Tag commit is not on main",
        )
        workflow = api(f"{base}/actions/workflows/ci.yml")
        require(
            workflow["path"] == WORKFLOW and workflow["state"] == "active" and positive(workflow["id"]),
            "The CI workflow is unavailable",
        )
        workflow_id = workflow["id"]
        runs, count = [], None
        for page in range(1, 11):
            response = api(
                f"{base}/actions/workflows/{workflow_id}/runs",
                {"event": "push", "branch": "main", "head_sha": expected_sha, "per_page": 100, "page": page},
            )
            total = response["total_count"]
            # GitHub caps filtered searches at 1,000 results. A capped inventory is not proof of its latest run.
            require(positive(total) and total < 1000, "CI inventory is absent or reaches the search limit")
            require(count is None or total == count, "CI inventory changed during pagination")
            count = total
            batch = response["workflow_runs"]
            require(isinstance(batch, list) and len(batch) == min(100, count - len(runs)), "Incomplete CI inventory")
            runs.extend(batch)
            if len(runs) == count:
                break
        require(len(runs) == count, "Incomplete CI inventory")
        for run in runs:
            run_identity(run, workflow_id, repository, expected_sha)
        require(
            len({run["id"] for run in runs}) == len(runs) and len({run["run_number"] for run in runs}) == len(runs),
            "Duplicate CI inventory entries",
        )
        latest = max(runs, key=lambda run: run["run_number"])
        # Refresh the run itself, not an older attempt or child job that happened to pass.
        current = api(f"{base}/actions/runs/{latest['id']}")
        run_identity(current, workflow_id, repository, expected_sha)
        require(
            current["id"] == latest["id"]
            and current["run_number"] == latest["run_number"]
            and current["run_attempt"] >= latest["run_attempt"],
            "CI run changed inconsistently",
        )
        require(
            current["status"] == "completed" and current["conclusion"] == "success",
            "Latest CI attempt did not succeed",
        )
        return current
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("Malformed GitHub evidence") from exc


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never send the read token to a location supplied by a response


class GitHubApi:
    def __init__(self, token):
        require(bool(token), "Missing GitHub authentication")
        self.token = token
        self.opener = build_opener(NoRedirects())

    def __call__(self, path, params=None):
        request = Request(
            "https://api.github.com" + path + ("?" + urlencode(params) if params else ""),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == 200, "GitHub did not return complete evidence")
                body = response.read(4 * 1024 * 1024 + 1)
            require(len(body) <= 4 * 1024 * 1024, "GitHub response exceeded the size limit")
            result = json.loads(body)
            require(isinstance(result, dict), "GitHub returned malformed evidence")
            return result
        except Exception as exc:
            raise RuntimeError("GitHub API request failed") from exc


def main():
    try:
        require(os.environ.get("GITHUB_EVENT_NAME") == "push", "Release requires a tag push")
        require(os.environ.get("GITHUB_REF_TYPE") == "tag", "Release requires a tag push")
        require(os.environ.get("GITHUB_API_URL") == "https://api.github.com", "Unexpected API origin")
        repository, commit = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
        run = check_release(GitHubApi(os.environ["GH_TOKEN"]), repository, os.environ["GITHUB_REF_NAME"], commit)
        link = f"https://github.com/{repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
        summary = (
            f"Release CI accepted: `{commit}` on `main`, push workflow `{WORKFLOW}`, "
            f"[run {run['id']}, attempt {run['run_attempt']}]({link}): completed / success.\n"
        )
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(summary)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"commit={commit}\n")
        print(summary.rstrip())
        return 0
    except Exception:
        # Do not print response bodies, untrusted tag data, authentication details or exception chains.
        print(
            "Release blocked: successful complete main CI for the exact tag commit could not be verified.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
