"""The term sync alerts of docs/src/content/docs/enterprise/term-sync.mdx loaded in a real Prometheus (the release
checklist's item 8, "A real Prometheus rule tried"): its three rules exactly as the page prints them, read from the page
itself, and Server operations' scrape job `latet` (docs/src/content/docs/concepts/server-operations.md) with one
target for each of two instances of a server. Instance A holds the master key's versions 1 and 2 and syncs the terms,
sealed with version 2; instance B, started after, holds version 1 alone: it can open none of them, so it never loads
them. Prometheus runs in Docker and reaches the instances' metrics sockets, which listen on 127.0.0.1 alone, at
host.docker.internal: Docker Desktop's own process takes those connections on this computer and opens them to its
loopback, so no socket listens on a network. Opt-in: LATET_PROVIDER_TESTS=1, with prom/prometheus:v3.16.0 pulled by
hand (README.md). LATET_PROMETHEUS_PAGE_FOR=1 waits the page's own `for: 5m` too, rather than a copy's shorter one."""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from starlette.testclient import TestClient

from latet.ee.settings import TERM_FILES, TermSyncSettings
from latet.server.operations import METRICS, server_app

from ..people.server_support import BY_HAND
from ..servers import MASTER_KEY, URL
from . import docker
from .conftest import PROVIDER_TESTS

pytestmark = PROVIDER_TESTS

IMAGE = "prom/prometheus:v3.16.0"
DOCS = Path(__file__).parents[3] / "docs" / "src" / "content" / "docs"
TERM_SYNC = DOCS / "enterprise" / "term-sync.mdx"
OPERATIONS = DOCS / "concepts" / "server-operations.md"


def _block(page: Path, start: str) -> str:
    """The page's YAML block that begins with ``start``, as the page prints it."""
    blocks = re.findall(r"```yaml\n(.*?)```", page.read_text("utf-8"), re.S)
    [found] = [block for block in blocks if block.startswith(start)]
    return found


def page_rules() -> str:
    return _block(TERM_SYNC, "groups:\n  - name: latet-terms\n")


def scrape_job() -> dict[str, Any]:
    [job] = yaml.safe_load(_block(OPERATIONS, "scrape_configs:\n"))["scrape_configs"]
    assert job["job_name"] == "latet"
    return job


@dataclass
class Instance:
    server: Any
    client: TestClient
    port: int
    serving: uvicorn.Server
    thread: threading.Thread

    @property
    def target(self) -> str:
        return f"host.docker.internal:{self.port}"

    def scrape(self, token: str) -> str:
        answer = httpx.get(f"http://127.0.0.1:{self.port}{METRICS}", headers={"authorization": f"Bearer {token}"})
        assert answer.status_code == 200, answer.text
        return answer.text

    def close(self) -> None:
        self.serving.should_exit = True
        self.thread.join(10)
        self.client.__exit__(None, None, None)


def _sample(text: str, name: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return float(line.rsplit(" ", 1)[1])
    return None


def _open(instances, keys: Path, port: int, token: Path) -> Instance:
    """One more instance of the server, with its own master key file and its metrics socket on 127.0.0.1:``port``,
    served as `latet server run` serves it: the instance's app, and the same app on the metrics socket."""
    instances.settings = instances.settings.model_copy(
        update={"master_key_file": keys, "metrics_port": port, "metrics_token_file": token}
    )
    server = instances.open()
    app = server_app(server)
    client = TestClient(app, base_url=URL, client=("203.0.113.7", 50000))
    client.__enter__()
    serving = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=serving.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not serving.started:
        assert time.monotonic() < deadline, "the metrics socket did not open"
        time.sleep(0.05)
    return Instance(server, client, port, serving, thread)


def _until(done, what: str, seconds: float) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        found = done()
        if found:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {seconds:g} s")
        time.sleep(1.0)


@pytest.fixture
def pair(instances, licensed, tmp_path, monkeypatch):
    """Instance A, which synced the terms with the master key's version 2, and instance B, started after it with
    version 1 alone; the metrics token they share."""
    monkeypatch.delenv("LATET_MASTER_KEY")  # each instance reads its own key file
    second = base64.b64encode(secrets.token_bytes(32)).decode()
    both, first = tmp_path / "master-a.key", tmp_path / "master-b.key"
    both.write_text(f"{MASTER_KEY}\n{second}\n")
    first.write_text(f"{MASTER_KEY}\n")
    token = secrets.token_hex(32)
    token_file = tmp_path / "metrics.token"
    token_file.write_text(token + "\n")
    from latet.server.db import Database
    from latet.server.master import MasterKey, check_master_key

    # The server first started with version 1 alone, as before a rotation: its check value is sealed with it. Version 2
    # then went into A's key file and not yet into B's.
    db = Database(instances.settings.database_url, schema=instances.schema)
    try:
        check_master_key(db, MasterKey([base64.b64decode(MASTER_KEY)]))
    finally:
        db.close()
    opened: list[Instance] = []
    try:
        a = _open(instances, both, docker.free_port(), token_file)
        opened.append(a)
        today = datetime.now(UTC).date()
        document = licensed.make(issued=str(today - timedelta(days=1)), expires=str(today + timedelta(days=300)))
        a.server.edition.install_license(document, actor=BY_HAND)
        a.server.edition.refresh()
        folder = a.server.data / TERM_FILES
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "customers.csv").write_text("company\nHalden Insurance\nKestrel Analytics\n")
        source = {"name": "customers", "kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}}
        section = TermSyncSettings.model_validate({"sources": [source]}).model_dump(mode="json")
        a.server.edition.settings_store.set_section("term_sync", section, actor=BY_HAND)
        a.server.edition.refresh()
        terms = a.server.edition.terms
        _until(lambda: terms.loaded and terms.live.count == 2, "instance A's sync", 60)
        b = _open(instances, first, docker.free_port(), token_file)
        opened.append(b)
        b.server.edition.refresh()
        yield a, b, token
    finally:
        for instance in reversed(opened):
            instance.close()
        if opened:
            (opened[0].server.data / TERM_FILES / "customers.csv").unlink(missing_ok=True)


@dataclass
class Prometheus:
    container: docker.Container
    folder: Path

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.container.ports[9090]}/api/v1"

    def get(self, what: str) -> Any:
        answer = httpx.get(f"{self.api}/{what}", timeout=10)
        answer.raise_for_status()
        return answer.json()["data"]

    def alerts(self) -> list[dict[str, Any]]:
        return self.get("alerts")["alerts"]

    def rules(self, text: str) -> None:
        """The rules file replaced by ``text``, and Prometheus told to read its configuration again (SIGHUP)."""
        (self.folder / "latet-terms.yml").write_text(text, "utf-8", newline="\n")
        docker._docker("cp", str(self.folder / "latet-terms.yml"), f"{self.container.name}:/etc/prometheus/")
        self.container.signal("HUP")


@pytest.fixture
def prometheus(pair, tmp_path):
    a, b, token = pair
    folder = tmp_path / "prometheus"
    folder.mkdir()
    job = scrape_job()
    job["static_configs"] = [{"targets": [a.target, b.target]}]
    assert job["authorization"]["credentials_file"] == "/etc/prometheus/latet-metrics.token"
    config = {
        "global": {"evaluation_interval": "15s"},  # the rules are evaluated every 15 s (Prometheus's default: 1 m)
        "rule_files": ["/etc/prometheus/latet-terms.yml"],
        "scrape_configs": [job],
    }
    (folder / "prometheus.yml").write_text(yaml.safe_dump(config, sort_keys=False), "utf-8", newline="\n")
    (folder / "latet-terms.yml").write_text(page_rules(), "utf-8", newline="\n")
    (folder / "latet-metrics.token").write_text(token + "\n", "utf-8", newline="\n")
    container = docker.start("prometheus", IMAGE, ports=[9090], files={"/etc/prometheus": folder})
    try:
        found = Prometheus(container, folder)
        docker.wait(lambda: httpx.get(f"{found.api}/status/buildinfo").status_code == 200, "Prometheus", container)
        yield found
    finally:
        container.remove()
        shutil.rmtree(folder, ignore_errors=True)


def _alert(alerts, name: str, instance: str) -> dict[str, Any] | None:
    return next((x for x in alerts if x["labels"]["alertname"] == name and x["labels"]["instance"] == instance), None)


def test_the_pages_rules_tell_an_instance_whose_master_key_lacks_the_terms_version(pair, prometheus):
    a, b, token = pair
    on_a = a.scrape(token)
    assert _sample(on_a, "latet_terms_masked") == 2.0 and _sample(on_a, "latet_terms_unreadable") == 0.0
    _until(lambda: _sample(b.scrape(token), "latet_terms_unreadable") == 2.0, "B's unreadable terms", 60)
    assert _sample(b.scrape(token), "latet_terms_masked") is None  # never loaded: left out of the scrape
    print(f"Prometheus {prometheus.get('status/buildinfo')['version']}")

    def both_up():
        targets = prometheus.get("targets")["activeTargets"]
        return len(targets) == 2 and all(t["health"] == "up" for t in targets) and targets

    targets = _until(both_up, "both targets scraped", 120)
    print([(t["labels"]["instance"], t["labels"]["job"], t["health"]) for t in targets])
    [group] = prometheus.get("rules")["groups"]
    assert group["name"] == "latet-terms" and [r["name"] for r in group["rules"]] == [
        "LatetTermsNeverLoaded", "LatetTermsBehind", "LatetTermsUnreadable",
    ], group  # fmt: skip

    def evaluated():  # a rule's health is "unknown" until its group's first evaluation (every 15 s here)
        [found] = prometheus.get("rules")["groups"]
        return all(r["health"] == "ok" for r in found["rules"]) and found

    _until(evaluated, "the rules' first evaluation", 60)

    def told():
        alerts = prometheus.alerts()
        unreadable = _alert(alerts, "LatetTermsUnreadable", b.target)
        never = _alert(alerts, "LatetTermsNeverLoaded", b.target)
        return (unreadable, never, alerts) if unreadable and never and unreadable["state"] == "firing" else None

    _unreadable, never, alerts = _until(told, "the alerts", 120)
    print("with the page's rules:", [(x["labels"]["alertname"], x["labels"]["instance"], x["state"]) for x in alerts])
    assert never["state"] == "pending"  # the page's `for: 5m`
    assert not [x for x in alerts if x["labels"]["instance"] == a.target]  # A masks its terms: nothing to say

    if os.environ.get("LATET_PROMETHEUS_PAGE_FOR") == "1":
        fired = _until(
            lambda: (x := _alert(prometheus.alerts(), "LatetTermsNeverLoaded", b.target)) and x["state"] == "firing"
            and x, "LatetTermsNeverLoaded firing with the page's own rules", 420,
        )  # fmt: skip
        print("with the page's own `for: 5m`:", fired["labels"]["alertname"], fired["state"], fired["activeAt"])
        return
    shorter = page_rules().replace("for: 5m", "for: 30s")
    assert shorter.replace("for: 30s", "for: 5m") == page_rules() and shorter != page_rules()  # the only change
    prometheus.rules(shorter)

    def firing():
        found = _alert(prometheus.alerts(), "LatetTermsNeverLoaded", b.target)
        return found if found and found["state"] == "firing" else None

    found = _until(firing, "LatetTermsNeverLoaded firing", 180)
    print("with `for: 30s`:", found["labels"]["alertname"], found["labels"]["instance"], found["state"])
    assert not [x for x in prometheus.alerts() if x["labels"]["instance"] == a.target]
