"""A computer without a server is the Community Edition whatever its folders hold (the user's decision of 2026-10-23:
the Enterprise Edition is a Latet server's alone): its edition, its dashboard, `latet status` and the command line
show nothing Enterprise, and its edition never imports the Enterprise code."""

import dataclasses
import sys
import time
import types
import typing
from contextlib import contextmanager
from types import SimpleNamespace

import typer.main
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.config import Config, IdentityConfig
from latet.edition import Community, EditionContext, load_edition
from latet.gateway.app import create_app
from latet.runtime import make_runtime

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
COMMUNITY = {"name": "community", "license": None, "features": [], "pages": [], "roles": [], "actions": []}


def _everything(tmp_path, licensed, monkeypatch):
    """Every Enterprise file E1 ever read on a computer, each valid: the data folder's license, the managed folder's
    license, policy and settings (LATET_MANAGED_DIR's), and an ee.yaml beside config.yaml."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "license.json").write_bytes(licensed.make(expires="2099-12-31"))
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "license.json").write_bytes(licensed.make(expires="2099-12-31"))
    (managed / "policy.yaml").write_text("latet_rules: 1\nprofile: max\n", "utf-8")
    (managed / "ee.yaml").write_text("{}\n", "utf-8")
    (tmp_path / "ee.yaml").write_text("{}\n", "utf-8")
    monkeypatch.setenv("LATET_MANAGED_DIR", str(managed))
    return data


@contextmanager
def _signed_in(rt):
    """The runtime's dashboard, signed in as a person signs in: a code from the bridge token, then the login page."""
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
        assert client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False).status_code == 303
        yield client


def test_a_computer_with_every_enterprise_file_is_the_community_edition(tmp_path, licensed, monkeypatch):
    data = _everything(tmp_path, licensed, monkeypatch)
    edition = load_edition(EditionContext(data=data, config_dir=tmp_path, server=None, clock=time.time))
    assert isinstance(edition, Community) and edition.me(None) == COMMUNITY and edition.checks() == []


def test_a_computer_never_imports_the_enterprise_code_for_its_edition(tmp_path, monkeypatch):
    broken = types.ModuleType("latet.ee")

    def missing(name):
        raise ModuleNotFoundError("No module named 'cryptography'", name="cryptography")

    broken.__getattr__ = missing
    monkeypatch.setitem(sys.modules, "latet.ee", broken)
    context = EditionContext(data=tmp_path / "data", config_dir=tmp_path, server=None, clock=time.time)
    # Never looked for: a computer's edition runs no Enterprise code, broken or not (the command line still refuses
    # to load a broken install, as it should).
    assert isinstance(load_edition(context), Community)


def test_a_computers_dashboard_is_the_community_edition_and_has_no_license_route(tmp_path, licensed, monkeypatch):
    data = _everything(tmp_path, licensed, monkeypatch)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=data, config_file=tmp_path / "config.yaml")
    try:
        assert isinstance(rt.edition, Community) and rt.floor_used is None
        with _signed_in(rt) as client:
            assert client.get(f"{API}/me").json()["edition"] == COMMUNITY
            for method in ("GET", "PUT", "DELETE"):
                answer = client.request(method, f"{API}/ee/license", headers=UI, json={"license": "{}"})
                assert answer.status_code == 404, method
    finally:
        rt.close()
    assert (data / "license.json").exists()  # left as it was: nothing reads it, nothing removes it


def test_latet_status_says_nothing_of_a_license_on_a_computer(tmp_path, licensed, monkeypatch):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app
    from latet.service import daemon

    data = _everything(tmp_path, licensed, monkeypatch)
    monkeypatch.setenv("LATET_DATA_DIR", str(data))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(daemon, "status", lambda *args, **kwargs: SimpleNamespace(running=False))
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 3 and "License" not in result.output and "Enterprise" not in result.output


def test_latet_license_is_no_command_and_the_servers_stay():
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    command = typer.main.get_command(app)
    assert "license" not in command.commands and "policy" in command.commands  # type: ignore[attr-defined]
    assert {"license", "policy"} <= set(command.commands["server"].commands)  # type: ignore[attr-defined]
    ran = CliRunner().invoke(app, ["license", "show"])
    assert ran.exit_code == 2 and "No such command" in ran.output


def test_nothing_of_a_computers_enterprise_side_is_left():
    from latet.ee import api, license, policy_sources, secrets, settings

    gone = {
        license: ("FileLicenses", "ManagedElsewhere", "PROTECTS", "MANAGED_ATTEMPTS", "LICENSE_FILE"),
        settings: ("FileSettings", "load_ee_settings", "save_ee_settings", "ee_settings_path", "ee_settings_in_use",
                   "managed_settings_path", "read_settings", "SETTINGS_FILE"),
        secrets: ("LocalSecrets", "SecretsError"),
        api: ("license_text", "ON_SERVER"),
        policy_sources: ("NoPolicy",),
    }  # fmt: skip
    assert {m.__name__: [n for n in names if hasattr(m, n)] for m, names in gone.items()} == {
        m.__name__: [] for m in gone
    }
    assert "held" not in {f.name for f in dataclasses.fields(license.Stored)}
    assert typing.get_args(license.Source) == ("server",)
