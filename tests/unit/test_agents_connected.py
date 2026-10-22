"""A computer connected to a Latet server gives its agents the server's addresses: every wrapper, `latet env`, the
routes `agents apply` writes and the checks of what is applied; the certificate it pinned where an agent reads one;
and its own calls (a destination, the user name's fake) go to the identity's space over HTTPS.

And what a connected computer keeps of it is kept where only the person reads it: nothing of the connection is read
from a folder others can change; Claude Code's settings hold this computer's token only where nobody else reads them,
said by every command that writes it; the space's address is on no command line; `latet disconnect` leaves no copy of
the token or of the space's address in what Latet wrote, even half-way."""

import datetime
import http.client
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import typer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.responses import JSONResponse, Response

from latet import wrappers
from latet.cli.main import _ensure_daemon
from latet.config import Config, data_dir, load_config
from latet.engine.reviewer import certificate as certificates
from latet.integrations import agents, routes
from latet.integrations.base import FileEdit, Record, RecordsUnavailable, load_records, save_records
from latet.integrations.routes import detect_claude, detect_codex, is_latet
from latet.server.certificate import make_certificate
from latet.server.client import pin_certificate
from latet.server.connection import (
    Connection,
    NotConnected,
    connection_dir,
    kept_tokens,
    load_connection,
    save_connection,
    save_server_token,
)
from latet.server.tokens import route_key

from .conftest import run_cli
from .fake_latet_server import free_port

URL = "https://latet.acme.lan"
TOKEN = "latet_pat_" + "z" * 43
ROUTE = route_key(TOKEN)
SPACE = f"{URL}/u/{ROUTE}"
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _certificate(name: str, authority: bool = False) -> str:
    """A self-signed certificate: a server's own, CA:FALSE with no name constraint (an administrator's own openssl
    line), or an authority (a company's inspection proxy)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    built = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=authority, path_length=None), critical=True)
    )
    if not authority:
        built = built.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    return built.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


PROXY_AUTHORITY = _certificate("Acme inspection proxy", authority=True)


def _constrained(
    host: str, *, authority: bool = False, critical: bool = True, permitted: list[x509.GeneralName] | None = None
) -> str:
    """A self-signed certificate for ``host`` with a name constraint that permits it alone, as `latet server
    certificate --make` writes it, but an authority, with the constraint not critical, or permitting ``permitted``,
    as asked."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.UTC)
    if permitted is None:
        permitted = [
            x509.DNSName(host),
            x509.IPAddress(ipaddress.ip_network("0.0.0.0/32")),
            x509.IPAddress(ipaddress.ip_network("::/128")),
        ]
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=authority, path_length=None), critical=True)
        .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=critical)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )


def _connect(url, fingerprint=None, token=TOKEN):
    connection = Connection(url=url, user="u_emilycarte", user_name="Emily Carter", fingerprint=fingerprint)
    save_server_token(data_dir(), connection, token)
    save_connection(connection)
    return connection


@pytest.fixture(autouse=True)
def _agents_here(computer, tmp_path, monkeypatch):
    """This computer's Latet in folders of the test (``computer``: your home is the test's too), the agents' settings
    in folders of the test (yours are never read), no organization's managed settings, and nothing of this shell's
    that names where an agent goes or what it trusts."""
    for name in ("claude", "codex", "opencode-config"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "opencode-config"))
    monkeypatch.setattr(routes, "MANAGED_SETTINGS", (tmp_path / "managed-settings.json",))
    for var in (*routes.ROUTE_VARS, "NODE_EXTRA_CA_CERTS", "CODEX_CA_CERTIFICATE", "LATET_BRIDGE_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def connected(computer):
    """This computer connected to the server, its self-signed certificate (`latet server certificate --make`'s,
    constrained to its host) pinned: gives that certificate."""
    crt, _, fingerprint = make_certificate(URL, computer / "tls")
    certificates.save(data_dir(), crt.read_text())
    _connect(URL, fingerprint)
    return crt.read_text()


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Claude Code's settings folder, its `claude plugin` doing to settings.json what it does, and no user name's
    fake asked of anyone."""

    def claude_cli(self, *args):
        if args[:2] == ("plugin", "install"):
            path = self.settings_path()
            data = json.loads(path.read_text("utf-8")) if path.exists() else {}
            data.setdefault("enabledPlugins", {})["latet@latet"] = True
            path.write_text(json.dumps(data, indent=2), "utf-8")
        return {"outcome": "ok"}

    monkeypatch.setattr(agents.ClaudeCode, "_claude", claude_cli)
    monkeypatch.setattr(wrappers, "user_name_surrogate", lambda config=None, **_kwargs: None)
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
    return tmp_path / "claude"


def _settings_env(folder: Path) -> dict:
    return json.loads((folder / "settings.json").read_text("utf-8")).get("env", {})


def _letters(text: str) -> str:
    """``text``'s letters and digits alone: what Typer's error box says, however it folded the lines and whatever
    colours it gave them (on GitHub Actions it colours its box, even for a test's runner)."""
    return re.sub(r"[^A-Za-z0-9]", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


# --- the addresses ---------------------------------------------------------------------------------------------------


def test_every_address_an_agent_gets_is_the_identitys_space(connected):
    env = wrappers.claude_env()
    assert (env["ANTHROPIC_BASE_URL"], env["LATET_BRIDGE_URL"], env["LATET_BRIDGE_TOKEN"]) == (
        f"{SPACE}/anthropic", SPACE, TOKEN,
    )  # fmt: skip
    assert wrappers.codex_base_url(chatgpt=False) == f"{SPACE}/openai/v1"
    assert all(p["options"]["baseURL"].startswith(SPACE) for p in wrappers.opencode_overrides()["provider"].values())
    assert wrappers.generic_env()["OPENAI_BASE_URL"] == f"{SPACE}/openai/v1"
    printed = run_cli("env", "--agent", "claude", "--shell", "bash").output
    assert f"export ANTHROPIC_BASE_URL='{SPACE}/anthropic'" in printed


def test_a_computer_that_is_not_connected_gets_the_addresses_it_always_had(computer):
    assert wrappers.base_url() == "http://127.0.0.1:5283" and wrappers.server_base() is None
    assert wrappers.claude_env()["LATET_BRIDGE_URL"] == "http://127.0.0.1:5283"
    assert all(not wrappers.extra_ca_env(agent) and wrappers.untrusting(agent) is None for agent in wrappers.AGENTS)


def test_an_api_keys_space_is_given_to_a_program_by_its_address(computer):
    env = wrappers.generic_env(base=f"{URL}/u/{route_key('latet_key_' + 'k' * 43)}")
    assert env["ANTHROPIC_BASE_URL"] == f"{URL}/u/{route_key('latet_key_' + 'k' * 43)}/anthropic"


# --- the certificate an agent is given (T1.1, T3.8, T5.7, T5.13, T5.14) ---------------------------------------------


@pytest.mark.parametrize("agent", sorted(wrappers.EXTRA_CA_VARS))
def test_an_agent_is_given_the_pinned_certificate_where_it_reads_one(connected, agent):
    given = wrappers.extra_ca_env(agent)
    assert set(given) == set(wrappers.EXTRA_CA_VARS[agent])
    copy = Path(next(iter(given.values())))
    assert copy.parent == connection_dir() and copy.read_text() == connected  # a copy of Latet's, never the store's
    assert (wrappers.untrusting(agent) is None) == bool(wrappers.EXTRA_CA_VARS[agent])


def test_nothing_is_said_of_certificates_without_a_pinned_one(computer):
    _connect(URL)  # a certificate of the company's authority, or a public one
    assert all(wrappers.untrusting(agent) is None and not wrappers.extra_ca_env(agent) for agent in wrappers.AGENTS)


def test_no_agent_is_given_a_pin_that_is_not_constrained_to_the_servers_host(computer):
    fingerprint = certificates.save(data_dir(), _certificate("latet.acme.lan"))  # CA:FALSE, no name constraint
    _connect(URL, fingerprint)
    for agent in wrappers.AGENTS:
        assert wrappers.extra_ca_env(agent) == {}
        words = wrappers.untrusting(agent) or ""
        assert "name constraint" in words and "latet server certificate --make" in words, agent
        assert "store" not in words  # never sent to the computer's store, which every program would trust


def test_a_pin_constrained_to_another_host_is_given_to_no_agent(computer):
    crt, _, fingerprint = make_certificate("https://latet.other.lan", computer / "other")
    certificates.save(data_dir(), crt.read_text())
    _connect(URL, fingerprint)
    assert all(wrappers.extra_ca_env(agent) == {} for agent in wrappers.AGENTS)


@pytest.mark.parametrize(
    "pem",
    [_constrained("latet.acme.lan", critical=False), _constrained("latet.acme.lan", authority=True)],
    ids=["constraint-not-critical", "authority"],
)
def test_a_pin_that_is_an_authority_or_whose_constraint_an_agent_may_skip_is_given_to_no_agent(computer, pem):
    _connect(URL, certificates.save(data_dir(), pem))
    assert all(wrappers.extra_ca_env(agent) == {} and wrappers.untrusting(agent) for agent in wrappers.AGENTS)


@pytest.mark.parametrize(
    ("host", "permitted"),
    [
        ("latet.acme.lan", [x509.DNSName("latet.acme.lan")]),  # no address permitted: any address left open
        ("10.0.0.5", [x509.IPAddress(ipaddress.ip_network("10.0.0.5/32"))]),  # no name permitted: any name left open
    ],
    ids=["name-alone", "address-alone"],
)
def test_a_constraint_that_leaves_one_kind_of_name_open_is_no_constraint(computer, host, permitted):
    _connect(f"https://{host}", certificates.save(data_dir(), _constrained(host, permitted=permitted)))
    assert all(wrappers.extra_ca_env(agent) == {} for agent in wrappers.AGENTS)
    words = wrappers.untrusting("codex", for_good=True) or ""
    assert "name constraint" in words and "store" not in words


def test_a_pin_latet_makes_for_an_address_is_given_to_every_agent(computer):
    crt, _, fingerprint = make_certificate("https://10.0.0.5", computer / "address")
    certificates.save(data_dir(), crt.read_text())
    _connect("https://10.0.0.5", fingerprint)
    assert all(wrappers.extra_ca_env(agent) for agent in wrappers.AGENTS)


def test_the_certificate_an_agent_reads_is_never_written_in_a_folder_others_can_change(connected):
    _open_to_others(connection_dir())
    with pytest.raises(NotConnected, match="not written"):
        wrappers.extra_ca_env("opencode")


def test_claude_codes_session_settings_are_never_written_in_a_folder_others_can_change(connected):
    folder = data_dir() / "claude-settings"
    _open_to_others(folder)
    with pytest.raises(NotConnected, match="session settings"):
        wrappers.claude_settings_file({"LATET_BRIDGE_TOKEN": TOKEN}, [])
    assert list(folder.glob("*.json")) == []


def test_untrusting_names_the_store_only_for_a_constrained_certificate_and_after_latets_own_way(connected):
    for agent in ("codex", "opencode"):  # they take no certificate from their own files
        words = wrappers.untrusting(agent, for_good=True) or ""
        assert f"latet {agent}" in words and "store" in words and "every program" in words, agent
        assert words.index(f"latet {agent}") < words.index("company") < words.index("store")
    assert wrappers.untrusting("claude", for_good=True) is None  # Claude Code reads it from its settings


def test_claude_codes_managed_settings_that_set_its_certificates_are_said(connected, tmp_path):
    managed = {"env": {"NODE_EXTRA_CA_CERTS": "C:/acme/ca.pem"}}
    (tmp_path / "managed-settings.json").write_text(json.dumps(managed), "utf-8")
    assert "managed settings" in (wrappers.untrusting("claude") or "")


def test_the_certificate_an_agent_reads_is_written_again_from_the_checked_pin(connected):
    copy = Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"])
    copy.write_text(connected + PROXY_AUTHORITY, "utf-8")  # an authority appended, as another user would
    again = Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"])
    assert again.read_text() == connected


def test_a_persons_own_certificates_are_given_with_the_pin_never_dropped(connected, computer, monkeypatch):
    theirs = computer / "proxy-ca.pem"
    theirs.write_text(PROXY_AUTHORITY, "utf-8")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(theirs))
    bundle = Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"]).read_text()
    assert bundle == PROXY_AUTHORITY + connected
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(theirs))
    assert Path(wrappers.extra_ca_env("codex")["CODEX_CA_CERTIFICATE"]).read_text() == PROXY_AUTHORITY + connected


def test_latets_own_certificates_are_never_bundled_again(connected, computer, monkeypatch):
    theirs = computer / "proxy-ca.pem"
    theirs.write_text(PROXY_AUTHORITY, "utf-8")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(theirs))
    first = wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"]  # the person's authority, then the pin
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", first)  # a shell that exported it (`latet env`), theirs dropped since
    assert Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"]).read_text() == connected
    fingerprint = certificates.fingerprint_of(connected)
    pinned = data_dir() / "certificates" / f"{fingerprint.removeprefix('sha256:')}.pem"
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(pinned))  # a pin of Latet's, named by the person
    assert Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"]).read_text() == connected


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (b"x" * (2 * 1024 * 1024), "1 MiB"),
        (b"0\x82\x01\x0a" + bytes(266), "no certificate"),  # DER, not PEM
        (b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", "no certificate"),
    ],
    ids=["too-large", "der", "a-key"],
)
def test_a_persons_file_that_is_no_certificate_is_left_out_and_said(connected, computer, monkeypatch, content, said):
    theirs = computer / "theirs.pem"
    theirs.write_bytes(content)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(theirs))
    words: list[str] = []
    given = wrappers.extra_ca_env("opencode", words)
    assert Path(given["NODE_EXTRA_CA_CERTS"]).read_text() == connected  # the pin all the same, alone
    assert any(str(theirs) in line and said in line for line in words), words


def test_a_persons_file_that_is_a_device_is_never_read(connected, monkeypatch):
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", os.devnull)
    words: list[str] = []
    assert Path(wrappers.extra_ca_env("opencode", words)["NODE_EXTRA_CA_CERTS"]).read_text() == connected
    assert any("plain file" in line for line in words), words


@POSIX
def test_a_persons_file_that_is_a_pipe_is_never_read(connected, computer, monkeypatch):
    pipe = computer / "pipe.pem"
    os.mkfifo(pipe)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(pipe))
    words: list[str] = []
    assert Path(wrappers.extra_ca_env("opencode", words)["NODE_EXTRA_CA_CERTS"]).read_text() == connected
    assert any("plain file" in line for line in words)


def test_a_certificate_the_person_set_in_claude_codes_settings_since_survives_a_re_apply(
    connected, claude_home, trusted_helper_install
):
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    settings = claude_home / "settings.json"
    mine = claude_home / "mine.pem"
    mine.write_text(PROXY_AUTHORITY, "utf-8")
    data = json.loads(settings.read_text("utf-8"))
    data["env"]["NODE_EXTRA_CA_CERTS"] = str(mine)
    settings.write_text(json.dumps(data), "utf-8")
    claude.apply(agents.ClaudeOptions())
    bundle = Path(_settings_env(claude_home)["NODE_EXTRA_CA_CERTS"])
    assert bundle.parent == connection_dir() and bundle.read_text() == PROXY_AUTHORITY + connected
    assert any("as they are now" in line for line in claude.said)
    claude.revert()
    assert _settings_env(claude_home)["NODE_EXTRA_CA_CERTS"] == str(mine)


# --- the routes ------------------------------------------------------------------------------------------------------


def test_claude_codes_route_goes_through_the_server(connected):
    company = {"shell": {"ANTHROPIC_BASE_URL": "https://llm.acme.com"}}
    route = detect_claude(company, Config(), TOKEN, lambda url: "llm-acme-com", None, SPACE)
    assert route.env == {"ANTHROPIC_BASE_URL": f"{SPACE}/llm-acme-com"}
    there = {"shell": {"ANTHROPIC_BASE_URL": f"{SPACE}/anthropic"}}
    assert detect_claude(there, Config(), TOKEN, str, None, SPACE).summary == "already through Latet"
    assert detect_claude(there, Config(), TOKEN, str, None, SPACE).kind == "anthropic"
    local = {"shell": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"}}  # a proxy on this computer
    assert "through the server" in (detect_claude(local, Config(), TOKEN, str, None, SPACE).problem or "")


def test_bedrock_is_never_signed_with_an_aws_profile_on_a_server(connected):
    profile = {"shell": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "work"}}
    assert "shell" in (detect_claude(profile, Config(), TOKEN, str, None, SPACE).problem or "")
    key = {"shell": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "ABSK" + "x" * 20}}
    assert detect_claude(key, Config(), TOKEN, str, None, SPACE).env["ANTHROPIC_BEDROCK_BASE_URL"].startswith(SPACE)


def test_what_aws_signing_on_a_server_says_of_the_shells_credentials(connected):
    shell = {
        "shell": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_ACCESS_KEY_ID": "AKIA" + "X" * 16, "AWS_SECRET_ACCESS_KEY": "s"}
    }
    refused = detect_claude(shell, Config(), TOKEN, str, None, SPACE).problem or ""
    assert "latet claude" in refused and "AWS_BEARER_TOKEN_BEDROCK" in refused and "profile" not in refused
    held = detect_claude(shell, Config(), TOKEN, str, lambda credentials: "0" * 32, SPACE)
    assert held.env["ANTHROPIC_BEDROCK_BASE_URL"] == f"{SPACE}/bedrock/us-east-1/session/{'0' * 32}"
    words = " ".join(held.summary.split())
    assert "the Latet server signs" in words and "Valkey" in words and "an hour" in words and "temporary" in words
    here = detect_claude(shell, Config(), TOKEN, str, None).problem or ""  # not connected: as it always was
    assert "AWS_PROFILE" in here and "Latet server" not in here
    assert "Latet server" not in detect_claude(shell, Config(), TOKEN, str, lambda credentials: "0" * 32).summary


def test_codex_goes_through_the_server(connected):
    route = detect_codex({"config.toml": ""}, Config(), True, str, address=SPACE)
    assert route.overrides["openai_base_url"] == f"{SPACE}/backend-api/codex"


LITELLM = (
    'model_provider = "litellm"\n\n[model_providers.litellm]\nname = "LiteLLM"\n'
    'base_url = "http://127.0.0.1:4000/v1"\nenv_key = "LITELLM_KEY"\n'
)


@pytest.mark.parametrize("text", [LITELLM, 'openai_base_url = "http://localhost:4000"\n'], ids=["provider", "openai"])
@pytest.mark.parametrize("session", [False, True], ids=["for-good", "session"])
def test_codex_going_to_this_computer_is_refused_on_a_server_and_left_with_a_note_otherwise(text, session):
    on_server = detect_codex({"config.toml": text}, Config(), False, str, session=session, address=SPACE)
    refused = on_server.problem or ""
    assert "on this computer" in refused and "through the server" in refused and "latet disconnect" in refused
    assert "latet config add upstreams" not in refused
    here = detect_codex({"config.toml": text}, Config(), False, str, session=session)
    assert here.problem is None and any("latet config add upstreams" in note for note in here.notes)


@pytest.mark.parametrize(
    "rest",
    [
        "/../theirs/anthropic",
        "/%2e%2e/theirs/anthropic",
        "/.%2E/theirs/anthropic",
        "/anthropic/../../theirs/anthropic",
        "/../../_latet/server/v1/admin/status#",
        "/anthropic/./v1",
        "//anthropic",
        "/anthropic?x=1",
        "/anthropic#here",
        "/anthropic%2F",
    ],
)
def test_an_address_that_leaves_the_space_as_an_agent_sends_it_is_not_latets(rest):
    assert not is_latet(SPACE + rest, 5283, SPACE)


@pytest.mark.parametrize(
    "url",
    [
        f"{SPACE}/anthropic",
        f"{SPACE}/anthropic/",
        SPACE,
        f"{SPACE}/",
        f"https://latet.acme.lan:443/u/{ROUTE}/anthropic",
    ],
)
def test_the_spaces_own_addresses_are_latets(url):
    assert is_latet(url, 5283, SPACE)


def test_another_space_or_server_is_not_latets():
    assert not is_latet(f"{URL}/u/{ROUTE[:-1]}x/anthropic", 5283, SPACE)
    assert not is_latet(f"https://latet.other.lan/u/{ROUTE}/anthropic", 5283, SPACE)
    assert not is_latet(f"http://latet.acme.lan/u/{ROUTE}/anthropic", 5283, SPACE)
    assert not is_latet("http://127.0.0.1:5283/anthropic", 5283, SPACE)  # this computer's Latet is not, there


@pytest.mark.parametrize("session", [False, True], ids=["for-good", "session"])
def test_codex_going_to_a_server_on_this_computer_goes_to_latet(computer, session):
    here = f"https://127.0.0.1:8443/u/{ROUTE}"  # the server on this computer: its space is Latet's, not a proxy
    route = detect_codex({"config.toml": f'openai_base_url = "{here}/openai/v1"\n'}, Config(), False, str,
                         session=session, address=here)  # fmt: skip
    assert route.problem is None and not any(route.edits.values())


def test_opencode_counts_as_applied_only_the_spaces_own_addresses(connected, tmp_path):
    path = tmp_path / "opencode-config" / "opencode" / "opencode.jsonc"
    path.parent.mkdir(parents=True)
    paths = {"anthropic": "/anthropic/v1", "openai": "/openai/v1", "openrouter": "/openrouter/api/v1"}

    def write(base: str) -> None:
        providers = {name: {"options": {"baseURL": f"{base}{rest}"}} for name, rest in paths.items()}
        path.write_text(json.dumps({"provider": providers}), "utf-8")

    write(f"{SPACE}/../theirs")  # holds the space's address, and leaves it as an agent sends it
    assert agents.OpenCode().status() != "applied"
    write(SPACE)
    assert agents.OpenCode().status() == "applied"


def test_an_address_that_leaves_the_space_is_judged_as_any_other(connected):
    sideways = {"shell": {"ANTHROPIC_BASE_URL": f"{SPACE}/../theirs/anthropic"}}
    route = detect_claude(sideways, Config(), TOKEN, lambda url: "named", None, SPACE)
    assert route.summary != "already through Latet" and route.env == {"ANTHROPIC_BASE_URL": f"{SPACE}/named"}


# --- this computer's own calls (T5.15) -------------------------------------------------------------------------------


def test_a_destination_is_registered_on_the_server_never_in_this_computers_settings(connected, computer, monkeypatch):
    def away(path, body, config=None, method="POST"):
        raise wrappers.DaemonError("the daemon did not answer (ConnectError)")

    monkeypatch.setattr(wrappers, "_daemon_call", away)
    with pytest.raises(agents.RouteProblem, match="Latet server"):
        agents.register_destination("https://llm.acme.com", "claude", Config())
    assert not (computer / "config" / "config.yaml").exists()


def test_a_destination_and_the_user_names_fake_are_asked_of_the_space_over_https(latet_server, monkeypatch):
    latet_server.tokens.add(TOKEN)
    pin_certificate(latet_server.url, latet_server.fingerprint)
    _connect(latet_server.url, latet_server.fingerprint)
    assert wrappers.destination_for("https://llm.acme.com", "claude") == "llm-acme-com"
    monkeypatch.setattr("latet.config.os_user_name", lambda: "emily")
    assert wrappers.user_name_surrogate() == "usr_4f2a"
    assert {("POST", "_latet/admin/destinations"), ("POST", "_latet/admin/user-name")} <= set(latet_server.calls)


@pytest.fixture
def space(latet_server):
    """This computer connected to the fake server, which takes its token."""
    latet_server.tokens.add(TOKEN)
    pin_certificate(latet_server.url, latet_server.fingerprint)
    _connect(latet_server.url, latet_server.fingerprint)
    return latet_server


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (JSONResponse({"names": ["../../x"]}), "not a destination's name"),
        (JSONResponse({"names": ["anthropic"]}), "not a destination's name"),
        (JSONResponse({"names": []}), "not a destination's name"),
        (Response(b"[" * 200_000 + b"]" * 200_000, media_type="application/json"), "not a JSON object"),
        (Response(b"{" + b" " * (1024 * 1024 + 10) + b"}", media_type="application/json"), "more than"),
        (JSONResponse({"error": "refused \x1b[31mred\x1b[0m \u202eevil"}, status_code=500), "refused"),
    ],
    ids=["path", "reserved", "none", "nested", "too-large", "control-characters"],
)
def test_a_destination_the_space_answers_is_taken_only_in_latets_shape(space, answer, said):
    space.space_answers["_latet/admin/destinations"] = answer
    with pytest.raises(wrappers.DaemonError) as refused:
        wrappers.destination_for("https://llm.acme.com", "claude")
    words = str(refused.value)
    assert said in words and "\x1b" not in words and "\u202e" not in words


@pytest.mark.parametrize("surrogate", ["a/b", "x" * 257, "line\nbreak", "", 'ab"c', 42])
def test_a_user_names_fake_the_space_answers_is_taken_only_in_latets_shape(space, surrogate):
    space.space_answers["_latet/admin/user-name"] = JSONResponse({"surrogate": surrogate})
    with pytest.raises(wrappers.DaemonError, match="not a user name's fake"):
        wrappers.user_name_surrogate()


def test_a_user_names_fake_the_space_wants_none_of_is_none(space):
    space.space_answers["_latet/admin/user-name"] = JSONResponse({"surrogate": None})
    assert wrappers.user_name_surrogate() is None


@pytest.mark.parametrize("session", ["../../x", "0" * 31, "A" * 32, "0" * 32 + " ", 7])
def test_an_aws_session_the_space_answers_is_taken_only_in_latets_shape(space, session):
    from latet.gateway.sigv4 import AwsCredentials

    space.space_answers["_latet/engine/v1/aws-sessions"] = JSONResponse({"id": session})
    with pytest.raises(wrappers.DaemonError, match="not a session"):
        wrappers.aws_session(AwsCredentials("AKIA" + "X" * 16, "secret", None, None))


def test_an_aws_session_is_held_over_https_at_the_spaces_address(space):
    space.space_answers["_latet/engine/v1/aws-sessions/" + "0" * 32 + "/hold"] = Response(b"x", status_code=200)
    assert wrappers.hold_aws_session("0" * 32) is not None
    assert ("GET", f"_latet/engine/v1/aws-sessions/{'0' * 32}/hold") in space.calls


@pytest.mark.parametrize("session", ["a b", "x\r\nHost: evil", "0" * 32])
def test_holding_a_session_that_cannot_be_held_holds_nothing_and_raises_nothing(computer, session):
    _connect(f"https://127.0.0.1:{free_port()}")  # nothing answers there
    assert wrappers.hold_aws_session(session) is None


def test_a_session_of_another_shape_is_never_sent(connected, monkeypatch):
    def sent(*args, **kwargs):
        pytest.fail("a session's id of another shape went into a request")

    monkeypatch.setattr(http.client.HTTPConnection, "request", sent)  # HTTPSConnection's too
    for session in ("a b", "x\r\nHost: evil", "../../x", "0" * 31):
        assert wrappers.hold_aws_session(session) is None


def test_a_session_http_cannot_send_holds_nothing(computer, monkeypatch):
    monkeypatch.setattr(wrappers, "_SESSION", re.compile(r".+", re.DOTALL))  # past the shape, as a server's id could be
    _connect(f"https://127.0.0.1:{free_port()}")
    assert wrappers.hold_aws_session("a b") is None  # http.client's InvalidURL, held as nothing


def test_holding_a_session_without_this_computers_token_holds_nothing(computer):
    connection = Connection(url=URL, user="u_emilycarte", user_name="Emily Carter")
    save_connection(connection)  # the connection, its token gone
    assert wrappers.hold_aws_session("0" * 32) is None


class _Moves(BaseHTTPRequestHandler):
    """This computer's daemon, as something else on its port might answer: every call sent elsewhere (to a path of its
    own, where a redirect followed would be seen, with the token it carried)."""

    seen: ClassVar[list[str]] = []

    def do_POST(self):
        self.seen.append(f"{self.path} {self.headers.get('x-latet-token') is not None}")
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(302)
        self.send_header("location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
        self.send_header("content-length", "0")
        self.end_headers()

    do_GET = do_POST

    def log_message(self, *args):
        pass


def test_a_call_to_this_computers_daemon_follows_no_redirect(computer):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Moves)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Moves.seen = []
    try:
        with pytest.raises(wrappers.DaemonError):
            wrappers.destination_for("https://llm.acme.com", "claude", Config(port=server.server_address[1]))
    finally:
        server.shutdown()
        server.server_close()
    assert _Moves.seen == ["/_latet/admin/destinations True"]  # asked once, the token sent nowhere else


class _Answers(BaseHTTPRequestHandler):
    """This computer's daemon, as something else on its port might answer: with ``body``."""

    body: ClassVar[bytes] = b"{}"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize(
    "body",
    [b"[" * 200_000 + b"]" * 200_000, json.dumps({"names": ["llm-acme-com"], "pad": "x" * 2_000_000}).encode()],
    ids=["nested", "too-large"],
)
def test_an_answer_of_this_computers_daemon_is_read_within_bounds(computer, body):
    _Answers.body = body
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Answers)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(wrappers.DaemonError):
            wrappers.destination_for("https://llm.acme.com", "claude", Config(port=server.server_address[1]))
    finally:
        server.shutdown()
        server.server_close()


def test_an_agent_is_not_started_while_the_server_does_not_answer(computer, capsys):
    _connect(f"https://127.0.0.1:{free_port()}")
    with pytest.raises(typer.Exit):
        _ensure_daemon()
    assert "cannot be reached" in capsys.readouterr().err


def test_an_agent_is_not_started_while_the_server_refuses_this_computers_token(latet_server, capsys):
    pin_certificate(latet_server.url, latet_server.fingerprint)
    _connect(latet_server.url, latet_server.fingerprint)  # a token the server does not take (revoked, say)
    with pytest.raises(typer.Exit):
        _ensure_daemon()
    said = capsys.readouterr().err  # R7: why, and what to do
    assert "no longer accepts this computer's token" in said and "`latet disconnect`, then `latet connect" in said


# --- what is applied for good (T5.8, T5.10) --------------------------------------------------------------------------


def test_what_is_applied_is_counted_with_the_servers_address(connected, tmp_path):
    settings = {"env": {"ANTHROPIC_BASE_URL": f"{SPACE}/anthropic"}, "enabledPlugins": {"latet@latet": True}}
    (tmp_path / "claude" / "settings.json").write_text(json.dumps(settings))
    assert agents.ClaudeCode().status() == "applied"
    (tmp_path / "codex" / "config.toml").write_text(f'openai_base_url = "{SPACE}/backend-api/codex"\n')
    assert agents.Codex().status() == "applied"


@WINDOWS
def test_claude_codes_settings_hold_the_token_and_every_writer_says_so(
    connected, claude_home, capsys, trusted_helper_install
):
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    env = _settings_env(claude_home)
    assert env["LATET_BRIDGE_TOKEN"] == TOKEN and env["ANTHROPIC_BASE_URL"] == f"{SPACE}/anthropic"
    line = f"{claude_home / 'settings.json'} now holds this computer's token for {URL}"
    assert any(line in said and "never sync or commit" in said for said in claude.said)
    from latet.cli.admin import refresh_applied

    capsys.readouterr()
    refresh_applied()  # `latet connect`'s, an upgrade's, a restore's, a port move's
    assert line in " ".join(capsys.readouterr().out.split())
    shown = run_cli("agents", "apply", "claude")
    assert shown.exit_code == 0 and line in " ".join(shown.output.split())


@POSIX
def test_on_posix_settings_that_hold_the_spaces_address_are_private_and_hold_no_token(connected, claude_home):
    """R5: no helper binding is made outside Windows in E0c, so no token is written where nothing could use it."""
    settings = claude_home / "settings.json"
    settings.write_text("{}", "utf-8")
    settings.chmod(0o644)
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    env = _settings_env(claude_home)
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600 and env["ANTHROPIC_BASE_URL"] == f"{SPACE}/anthropic"
    assert "LATET_BRIDGE_TOKEN" not in env and "LATET_BRIDGE_HELPER" not in env
    assert any("needs a Windows terminal in this version" in line for line in claude.said)
    assert not any("now holds this computer's token" in line for line in claude.said)


def _junction(link: Path, target: Path) -> bool:
    """``link``, a folder that leads to ``target`` (a junction on Windows, a symbolic link elsewhere)."""
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


def test_settings_in_a_linked_folder_are_left_as_they_are(connected, claude_home, tmp_path, monkeypatch):
    real = tmp_path / "dotfiles" / "claude"
    real.mkdir(parents=True)
    (real / "settings.json").write_text('{"env": {"MY_OWN": "1"}}', "utf-8")
    if not _junction(tmp_path / "linked", real):
        pytest.skip("this computer does not let a test make a link")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "linked"))
    with pytest.raises(agents.RouteProblem, match="link") as refused:
        agents.ClaudeCode().apply(agents.ClaudeOptions())
    assert "latet claude" in str(refused.value)
    assert (real / "settings.json").read_text("utf-8") == '{"env": {"MY_OWN": "1"}}'  # neither token nor address


def test_a_settings_file_that_is_a_link_is_left_as_it_is(connected, claude_home, tmp_path):
    real = tmp_path / "dotfiles.json"
    real.write_text("{}", "utf-8")
    try:
        (claude_home / "settings.json").symlink_to(real)
    except OSError:
        pytest.skip("this computer does not let a test make a link")
    with pytest.raises(agents.RouteProblem, match="link"):
        agents.ClaudeCode().apply(agents.ClaudeOptions())
    assert real.read_text("utf-8") == "{}"


@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_codex_and_opencode_files_in_a_linked_folder_are_left_as_they_are(connected, claude_home, tmp_path,
                                                                          monkeypatch, agent):  # fmt: skip
    real = tmp_path / "dotfiles" / agent
    real.mkdir(parents=True)
    if not _junction(tmp_path / f"linked-{agent}", real):
        pytest.skip("this computer does not let a test make a link")
    if agent == "codex":
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / f"linked-{agent}"))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / f"linked-{agent}"))
        (real / "opencode").mkdir()
        real = real / "opencode"
    applying = (
        (lambda: agents.Codex().apply(load_config(), chatgpt=False)) if agent == "codex" else agents.OpenCode().apply
    )
    with pytest.raises(agents.RouteProblem, match="link"):
        applying()
    assert list(real.iterdir()) == []  # nothing written there, through the link


@POSIX
def test_codex_files_that_are_links_are_left_as_they_are(connected, claude_home, tmp_path):
    real = tmp_path / "dotfiles.toml"
    real.write_text("", "utf-8")
    (tmp_path / "codex" / "config.toml").symlink_to(real)
    with pytest.raises(agents.RouteProblem, match="link"):
        agents.Codex().apply(load_config(), chatgpt=False)
    assert real.read_text("utf-8") == ""


def test_settings_in_a_folder_others_can_change_never_get_the_token(connected, claude_home, monkeypatch):
    monkeypatch.setattr(agents, "private_folder_problem", lambda folder: f"{folder} is open to others: chmod 700")
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    assert "LATET_BRIDGE_TOKEN" not in _settings_env(claude_home)
    said = "open to others" if sys.platform == "win32" else "needs a Windows terminal"  # R5 says the platform first
    assert any(said in line for line in claude.said)
    if sys.platform != "win32":  # the space's address it holds: the file private all the same (T5.25)
        assert stat.S_IMODE((claude_home / "settings.json").stat().st_mode) == 0o600


@WINDOWS
def test_a_roaming_windows_profile_never_gets_the_token(connected, claude_home, monkeypatch):
    monkeypatch.setattr(agents, "_roaming", lambda folder: "your Windows profile roams, and the file with it")
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    assert "LATET_BRIDGE_TOKEN" not in _settings_env(claude_home) and any("roams" in line for line in claude.said)


@WINDOWS
def test_this_computers_profile_is_read_for_whether_it_roams(tmp_path):
    assert agents._roaming(tmp_path) is None  # a test's folder, in a profile that does not roam


def test_latet_env_never_prints_the_token(connected):
    shown = run_cli("env", "--agent", "claude", "--shell", "bash")
    assert shown.exit_code == 0 and "latet_pat_" not in shown.output
    assert f"export LATET_BRIDGE_URL='{SPACE}'" in shown.output and "latet agents apply claude" in shown.output


@pytest.mark.parametrize("command", [["agents", "merge", "claude"], ["agents", "print"]], ids=["merge", "print"])
def test_the_containers_commands_give_out_nothing_of_a_connected_computer(connected, command):
    done = run_cli(*command, input='{"env": {}}')
    assert done.exit_code == 2 and "latet agents apply" in done.output
    assert "latet_pat_" not in done.output and ROUTE not in done.output


def test_codex_and_opencode_files_hold_the_spaces_address_and_say_it(connected, claude_home, tmp_path):
    codex, opencode = agents.Codex(), agents.OpenCode()
    codex.apply(load_config(), chatgpt=False)
    opencode.apply()
    config = tmp_path / "codex" / "config.toml"
    assert f"{SPACE}/openai/v1" in config.read_text("utf-8")
    assert any(str(config) in line and "never sync or commit" in line for line in codex.said)
    assert any("never sync or commit" in line for line in opencode.said)
    if sys.platform != "win32":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert stat.S_IMODE(opencode.config_path().stat().st_mode) == 0o600


@POSIX
def test_latets_record_of_what_it_applied_is_private(computer):
    save_records({})
    assert stat.S_IMODE((data_dir() / "integrations.json").stat().st_mode) == 0o600


def test_latet_codex_gives_the_spaces_address_in_a_private_profile_file_never_on_its_command_line(
    connected, tmp_path, monkeypatch
):
    home = tmp_path / "codex"
    (home / "work.config.toml").write_text('model = "gpt-5.5"\n', "utf-8")
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    monkeypatch.setattr("latet.cli.main._require", lambda command: None)
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda: None)
    seen = {}

    def launch(argv, env):
        name = argv[argv.index("--profile") + 1]
        path = home / f"{name}.config.toml"
        seen.update(argv=argv, env=env, path=path, text=path.read_text("utf-8"), mode=stat.S_IMODE(path.stat().st_mode))
        return 0

    monkeypatch.setattr(wrappers, "run", launch)
    from latet.cli.main import _codex

    assert _codex(["--profile", "work", "exec", "hi"]) == 0
    assert not any("/u/" in arg for arg in seen["argv"]) and "work" not in seen["argv"]
    assert re.fullmatch(r"latet-[0-9a-f]{16}", seen["argv"][seen["argv"].index("--profile") + 1])
    assert f'openai_base_url = "{SPACE}/openai/v1"' in seen["text"] and 'model = "gpt-5.5"' in seen["text"]
    assert seen["env"]["CODEX_CA_CERTIFICATE"] and not seen["path"].exists()  # gone when Codex ends
    if sys.platform != "win32":
        assert seen["mode"] == 0o600


@pytest.mark.parametrize(
    ("given", "named", "rest", "at"),
    [
        (["exec", "--profile", "work", "hi"], "work", ["exec", "hi"], 1),
        (["exec", "--profile=work", "hi"], "work", ["exec", "hi"], 1),
        (["exec", "-p", "work", "hi"], "work", ["exec", "hi"], 1),
        (["exec", "-pwork", "hi"], "work", ["exec", "hi"], 1),
        (["exec", "-p=work", "hi"], "work", ["exec", "hi"], 1),
        (["-m", "-p", "exec", "hi"], None, ["-m", "-p", "exec", "hi"], None),  # a model named -p, as clap reads it
        (["exec", "--", "-pwork"], None, ["exec", "--", "-pwork"], None),  # the prompt's, past --
        (["hi"], None, ["hi"], None),
    ],
)
def test_codex_profile_is_read_in_each_form_codex_takes(given, named, rest, at):
    assert wrappers.codex_profile(given) == (named, rest, at)


@pytest.mark.parametrize(
    "given",
    [["exec", "-p", "work", "--profile", "home", "hi"], ["exec", "-p"], ["exec", "--profile="], ["-p", "../x", "hi"]],
    ids=["two", "none-after", "empty", "not-a-name"],
)
def test_a_profile_latet_cannot_read_refuses_the_start(given):
    with pytest.raises(wrappers.CodexArgs):
        wrappers.codex_profile(given)


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        (["say hi"], ["--profile", "P", "say hi"]),  # the interactive session
        (["exec", "--skip-git-repo-check", "say hi"], ["exec", "--profile", "P", "--skip-git-repo-check", "say hi"]),
        (["-m", "gpt-5.5", "exec", "hi"], ["-m", "gpt-5.5", "exec", "--profile", "P", "hi"]),
        (["-pwork", "exec", "hi"], ["exec", "--profile", "P", "hi"]),  # the person's, before the subcommand
        (["exec", "-pwork", "--skip-git-repo-check", "hi"], ["exec", "--profile", "P", "--skip-git-repo-check", "hi"]),
        (["resume", "--last"], ["resume", "--profile", "P", "--last"]),
        (["exec", "resume", "--last", "hi"], ["exec", "--profile", "P", "resume", "--last", "hi"]),
        (["-m", "gpt-5.5", "-p", "work", "hi"], ["-m", "gpt-5.5", "--profile", "P", "hi"]),  # where the person's was
    ],
)
def test_latets_profile_goes_where_codex_reads_it(tmp_path, monkeypatch, given, sent):
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
    profile = tmp_path / "P.config.toml"
    assert wrappers.codex_argv(given, {}, exe="codex", profile_file=profile) == (["codex", *sent], {})


@pytest.mark.parametrize("given", [["review"], ["login"], ["exec", "-p"]], ids=["review", "login", "unreadable"])
def test_latet_codex_on_a_connected_computer_refuses_what_takes_no_profile(tmp_path, monkeypatch, given):
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
    with pytest.raises(wrappers.CodexArgs):
        wrappers.codex_argv(given, {}, exe="codex", profile_file=tmp_path / "P.config.toml")


def test_a_persons_attached_profile_is_read_for_where_codex_goes(connected, tmp_path, monkeypatch):
    (tmp_path / "codex" / "work.config.toml").write_text('openai_base_url = "http://127.0.0.1:4000/v1"\n', "utf-8")
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 162, 0))
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    monkeypatch.setattr("latet.cli.main._require", lambda command: None)
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda: None)
    monkeypatch.setattr(wrappers, "run", lambda argv, env: pytest.fail("Codex was started"))
    from latet.cli.main import _codex

    assert _codex(["exec", "-pwork", "hi"]) == 2  # work's local proxy, read and refused on a server (T5.12)


def test_global_install_on_a_connected_computer_routes_the_agents_only(connected, monkeypatch):
    for agent in agents.ALL.values():  # on the class: an instance's patch would leave its method behind when undone
        info = agents.AgentInfo(agent.id, agent.name, False, None, "")
        monkeypatch.setattr(type(agent), "detect", lambda self, info=info: info)
    done = run_cli("global-install", "--yes")
    assert done.exit_code == 0, done.output
    assert f"connected to {URL}" in done.output and "Start at login" not in done.output


def test_global_install_on_a_connected_computer_says_where_the_token_is(
    connected, claude_home, monkeypatch, trusted_helper_install
):
    for agent in agents.ALL.values():
        installed = agent.id == "claude"
        info = agents.AgentInfo(agent.id, agent.name, installed, "2.1" if installed else None, "")
        monkeypatch.setattr(type(agent), "detect", lambda self, info=info: info)
    done = run_cli("global-install", "--yes")
    assert done.exit_code == 0, done.output
    said = " ".join(done.output.split())
    if sys.platform == "win32":
        assert "now holds this computer's token" in said
    else:  # R5: said up front, and no token where no helper binding can use it
        assert "needs a Windows terminal in this version" in said and "does not hold this computer's token" in said


# --- a connection kept in a folder others can change (T5.7) ----------------------------------------------------------


def _open_to_others(folder: Path) -> None:
    """``folder`` as one others may change: 0777 on POSIX; on Windows, Users granted write."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(
            ["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)W"], check=True, capture_output=True, timeout=30
        )
    else:
        folder.chmod(0o777)


@contextmanager
def _no_unsafe_record_access(monkeypatch):
    """Observe the existing readers, enumeration and Windows rename without bypassing their guards."""
    from latet.cli import connect as command

    protected = [
        data_dir() / name
        for name in ("integrations.json", "backups", "integrations.json.untrusted", "backups.untrusted")
    ]
    protected += [connection_dir(), data_dir() / "claude-settings"]
    attempted = []

    def checked(name):
        if isinstance(name, (Path, str, bytes)):
            path = Path(os.fsdecode(name))
            if any(path == root or root in path.parents for root in protected):
                attempted.append(path)
                pytest.fail("unsafe record access")

    opened, descriptor_open = Path.open, os.open
    traversed, listed, renamed, read = Path.rglob, Path.iterdir, Path.rename, command._read_regular

    def checked_open(path, *args, **kwargs):
        checked(path)
        return opened(path, *args, **kwargs)

    def checked_descriptor(name, *args, **kwargs):
        checked(name)
        return descriptor_open(name, *args, **kwargs)

    def checked_rglob(path, *args, **kwargs):
        checked(path)
        return traversed(path, *args, **kwargs)

    def checked_iterdir(path):
        checked(path)
        return listed(path)

    def checked_rename(path, destination):
        if os.name == "nt":
            checked(path)
            checked(destination)
        return renamed(path, destination)

    def checked_read(path):
        checked(path)
        return read(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", checked_open)
        scoped.setattr(os, "open", checked_descriptor)
        scoped.setattr(Path, "rglob", checked_rglob)
        scoped.setattr(Path, "iterdir", checked_iterdir)
        scoped.setattr(Path, "rename", checked_rename)
        scoped.setattr(command, "_read_regular", checked_read)
        yield
    assert attempted == [], "the CLI swallowed an unsafe-access trap"


def test_nothing_of_a_connection_is_read_from_a_folder_others_can_change(connected, claude_home, monkeypatch):
    connection_path = data_dir() / "connection.yaml"
    connection_before = connection_path.read_bytes()
    _open_to_others(data_dir())
    with pytest.raises(NotConnected, match=re.escape(str(data_dir()))):
        load_connection()
    with pytest.raises(NotConnected):
        wrappers.extra_ca_env("claude")
    with pytest.raises(NotConnected):
        wrappers.base_url()
    launched = []
    monkeypatch.setattr(wrappers, "run", lambda argv, env: launched.append(argv) or 0)
    monkeypatch.setattr("latet.cli.main._require", lambda command: None)
    refused = run_cli("claude")
    assert refused.exit_code == 2 and not launched and _letters(str(data_dir())) in _letters(refused.output)
    monkeypatch.setattr(agents.ClaudeCode, "apply", lambda *a, **kw: pytest.fail("agent apply reached"))
    with _no_unsafe_record_access(monkeypatch):
        applied = run_cli("agents", "apply", "claude")
        with pytest.raises(RecordsUnavailable):
            load_records()
    assert applied.exit_code == 1 and "Integration records unavailable" in applied.output
    assert "LATET_DATA_DIR" in applied.output and not (claude_home / "settings.json").exists()
    assert not (data_dir() / "integrations.json").exists() and connection_path.read_bytes() == connection_before


def test_a_connection_file_kept_elsewhere_is_read_only_from_a_folder_of_the_persons_alone(
    connected, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "shared"
    _open_to_others(elsewhere)
    (elsewhere / "connection.yaml").write_bytes((data_dir() / "connection.yaml").read_bytes())
    monkeypatch.setenv("LATET_CONNECTION", str(elsewhere / "connection.yaml"))
    with pytest.raises(NotConnected, match=re.escape(str(elsewhere))):
        load_connection()


def test_connect_refuses_before_anything_is_sent_into_a_folder_others_can_change(latet_server, quiet, keyring_on):
    from .conftest import connect_to

    _open_to_others(data_dir())  # the keyring would keep the token: the connection file would be there all the same
    done = connect_to(latet_server)
    assert done.exit_code == 2 and _letters(str(data_dir())) in _letters(done.output)
    assert latet_server.asked == [] and load_connection(unchecked=True) is None
    assert certificates.load(data_dir(), latet_server.fingerprint) is None  # nothing pinned either


# --- latet disconnect leaves no copy of the token or of the space's address (T5.9) ------------------------------------


@pytest.fixture
def applied(latet_server, quiet, claude_home, tmp_path, monkeypatch, trusted_helper_install):
    """This computer connected to the fake server, Claude Code, Codex and OpenCode applied for good there, and a
    session of `latet claude` that crashed, its settings file left behind."""
    from .conftest import connect_to

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # `quiet` removed it, with every CODEX_* variable
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda exe="codex": False)
    assert Path.home() == tmp_path / "home" and agents.Codex().config_path().parent == tmp_path / "codex"
    assert connect_to(latet_server).exit_code == 0
    connection = load_connection()
    token = kept_tokens(data_dir())[0].token
    (claude_home / "settings.json").write_text(json.dumps({"env": {"MY_OWN": "1"}}), "utf-8")  # yours, before
    agents.ClaudeCode().apply(agents.ClaudeOptions())
    agents.Codex().apply(load_config(), chatgpt=False)
    agents.OpenCode().apply()
    crashed = data_dir() / "claude-settings" / "0123456789abcdef.json"
    crashed.parent.mkdir(exist_ok=True)
    crashed.write_text(json.dumps({"env": {"LATET_BRIDGE_TOKEN": token}}), "utf-8")
    files = [
        tmp_path / "claude" / "settings.json",
        tmp_path / "codex" / "config.toml",
        agents.OpenCode().config_path(),
        data_dir() / "integrations.json",
    ]
    secrets = (token, route_key(token))
    assert all(any(secret in path.read_text("utf-8") for secret in secrets) for path in files)
    return {"connection": connection, "token": token, "files": files, "crashed": crashed, "secrets": secrets}


def _left(applied) -> list[str]:
    """Each file of Latet's or of an agent's that still holds the token or the space's route key."""
    found = []
    folders = [data_dir(), *{path.parent for path in applied["files"]}]
    for folder in folders:
        for path in folder.rglob("*"):
            if path.is_file() and any(secret.encode() in path.read_bytes() for secret in applied["secrets"]):
                found.append(str(path))
    return sorted(set(found) - {str(data_dir() / "server.token")})


def test_no_file_an_agent_reads_holds_the_computer_key(applied, latet_server, tmp_path):
    """R16: Claude Code's settings, Codex's and OpenCode's files, `latet claude`'s session file, the helper's binding
    and Latet's records hold the token or the space's address; the computer key is in the token's own record alone."""
    [key] = latet_server.keys.values()
    assert all(any(secret in path.read_text("utf-8") for secret in applied["secrets"]) for path in applied["files"])
    holding = sorted(str(path) for path in tmp_path.rglob("*") if path.is_file() and key.encode() in path.read_bytes())
    assert holding == [str(data_dir() / "server.token")]  # LATET_KEYRING is off here: the private token file
    env = wrappers.claude_env()
    assert env["LATET_BRIDGE_TOKEN"] == applied["token"] and key not in json.dumps(env)


def test_disconnect_puts_every_agent_back_and_leaves_nothing_of_the_server(applied, latet_server, tmp_path):
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 0, done.output
    assert _left(applied) == [] and not applied["crashed"].exists() and load_connection() is None
    env = _settings_env(tmp_path / "claude")
    assert env["LATET_BRIDGE_URL"] == "http://127.0.0.1:5283"  # back to this computer's Latet
    assert "127.0.0.1:5283" in (tmp_path / "codex" / "config.toml").read_text("utf-8")
    words = " ".join(done.output.split())
    assert "Sessions running now keep this computer's token" in words and applied["token"] not in latet_server.tokens


def test_an_agent_that_cannot_be_put_back_is_cleaned_of_the_servers_values(
    applied, latet_server, tmp_path, monkeypatch
):
    def broken(self):
        raise FileNotFoundError("claude")  # its CLI gone, say

    monkeypatch.setattr(agents.ClaudeCode, "revert", broken)
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 1, done.output
    assert _left(applied) == []
    settings = tmp_path / "claude" / "settings.json"
    assert str(settings) in " ".join(done.output.split())
    again = run_cli("disconnect", "--yes")
    assert again.exit_code == 0 and "not connected" in again.output


def test_a_file_the_server_values_cannot_be_taken_out_of_keeps_the_connection_until_it_can(
    applied, latet_server, tmp_path, monkeypatch
):
    def broken(self):
        raise ValueError("not JSON")

    settings = tmp_path / "claude" / "settings.json"
    settings.write_text(settings.read_text("utf-8") + "\n} not JSON", "utf-8")  # nothing can be read out of it
    with monkeypatch.context() as patched:
        patched.setattr(agents.ClaudeCode, "revert", broken)
        done = run_cli("disconnect", "--yes")
    words = " ".join(done.output.split())
    assert done.exit_code == 1 and str(settings) in words and "latet disconnect" in words
    assert load_connection() is not None  # kept, so that it can be run again
    settings.write_text("{}", "utf-8")  # the person takes it out
    again = run_cli("disconnect", "--yes")
    assert again.exit_code == 0, again.output
    assert load_connection() is None and _left(applied) == []
    assert _settings_env(tmp_path / "claude")["LATET_BRIDGE_URL"] == "http://127.0.0.1:5283"  # applied again
    assert "127.0.0.1:5283" in (tmp_path / "codex" / "config.toml").read_text("utf-8")  # applied again too


def test_an_agent_that_cannot_be_applied_again_stays_without_the_server(applied, latet_server, tmp_path, monkeypatch):
    real = agents.ClaudeCode.apply

    def refused(self, options=None, config=None):
        if load_connection() is None:  # applied again to this computer's Latet
            raise agents.RouteProblem("your organization's managed settings send Claude Code elsewhere")
        return real(self, options, config)

    monkeypatch.setattr(agents.ClaudeCode, "apply", refused)
    done = run_cli("disconnect", "--yes")
    words = " ".join(done.output.split())
    assert done.exit_code == 1 and "latet agents apply claude" in words and _left(applied) == []
    assert "claude" not in load_records() and _settings_env(tmp_path / "claude") == {"MY_OWN": "1"}  # yours, as it was


def test_a_backup_that_holds_the_token_keeps_the_connection_and_is_named(applied, latet_server):
    kept = data_dir() / "backups" / "20261016-120000" / "settings.json.0123abcd"
    kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private, as Latet makes its backups
    kept.write_text(json.dumps({"env": {"LATET_BRIDGE_TOKEN": applied["token"]}}), "utf-8")
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 1 and str(kept) in " ".join(done.output.split()) and load_connection() is not None


def test_a_token_the_server_no_longer_takes_is_said_with_the_revoke_command(applied, latet_server):
    latet_server.tokens.discard(applied["token"])  # its identity disabled, say: enabled again, the token comes back
    done = run_cli("disconnect", "--yes")
    words = " ".join(done.output.split())
    assert done.exit_code == 0 and "comes back when the identity is enabled" in words
    assert "latet server people tokens u_emilycarte --revoke" in words and "did not revoke the token" in words


def test_the_check_looks_for_the_spaces_address_when_no_copy_of_the_token_is_known(applied, latet_server):
    (data_dir() / "server.token").unlink()  # the only copy: not here any more, the connection kept
    kept = data_dir() / "backups" / "20261016-120000" / "settings.json.0123abcd"
    kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private, as Latet makes its backups
    kept.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": f"{latet_server.url}/u/{route_key(applied['token'])}"}}))
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 1 and str(kept) in " ".join(done.output.split()) and load_connection() is not None


def test_the_check_knows_a_token_by_its_shape(applied, latet_server):
    kept = data_dir() / "backups" / "20261016-120000" / "settings.json.4567cdef"
    kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private, as Latet makes its backups
    kept.write_text(json.dumps({"env": {"LATET_BRIDGE_TOKEN": "latet_pat_" + "q" * 43}}), "utf-8")  # one not read here
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 1 and str(kept) in " ".join(done.output.split()) and load_connection() is not None


def test_a_run_stopped_while_the_agents_are_applied_again_is_taken_up_again(
    applied, latet_server, tmp_path, monkeypatch
):
    def stopped(self, config=None):
        raise KeyboardInterrupt  # Ctrl+C, while the agents are applied again

    with monkeypatch.context() as patched:
        patched.setattr(agents.OpenCode, "apply", stopped)
        run_cli("disconnect", "--yes")
    assert load_connection() is None and (connection_dir() / "disconnecting.json").is_file()
    again = run_cli("disconnect", "--yes")
    assert again.exit_code == 0, again.output
    assert "127.0.0.1:5283" in agents.OpenCode().config_path().read_text("utf-8")  # applied again, this time
    assert not (connection_dir() / "disconnecting.json").exists()


def test_disconnect_says_where_else_the_address_may_be(applied, latet_server):
    words = " ".join(run_cli("disconnect", "--yes").output.split())
    assert "shell profile" in words


def test_latet_env_says_the_address_is_a_secret(connected):
    shown = run_cli("env", "--shell", "bash")
    assert f"export OPENAI_BASE_URL='{SPACE}/openai/v1'" in shown.output
    assert "secret like this computer's token" in " ".join(shown.output.split())


# --- latet disconnect on a folder others can change (T5.24) ------------------------------------------------------------


def test_disconnect_trusts_nothing_of_a_folder_others_can_change(applied, latet_server, tmp_path, monkeypatch):
    applied["crashed"].unlink()  # untrusted folders' contents are left for the person (T5.26)
    applied["crashed"].parent.rmdir()
    note = connection_dir() / "disconnecting.json"
    note.parent.mkdir(exist_ok=True)
    planted = {"claude": {"extra_env": {"HTTPS_PROXY": "http://198.51.100.7:8080", "NODE_EXTRA_CA_CERTS": "/x.pem"}}}
    note.write_text(json.dumps(planted), "utf-8")  # another user's, as they could write it there
    manifest = data_dir() / "integrations.json"
    manifest_before = manifest.read_bytes()
    connection_path = data_dir() / "connection.yaml"
    connection_before = connection_path.read_bytes()
    _open_to_others(data_dir())
    with _no_unsafe_record_access(monkeypatch):
        done = run_cli("disconnect", "--yes")
    words = " ".join(done.output.split())
    assert "unrevoked" in words  # its token file there is never read (T3.11)
    if os.name == "nt":
        assert done.exit_code == 1 and "integration records retained" in words
        assert manifest.read_bytes() == manifest_before and not manifest.with_suffix(".json.untrusted").exists()
        assert not (data_dir() / "backups.untrusted").exists()
        assert connection_path.read_bytes() == connection_before
    else:
        assert load_connection(unchecked=True) is None
        assert not manifest.exists() and manifest.with_suffix(".json.untrusted").read_bytes() == manifest_before
    settings = (tmp_path / "claude" / "settings.json").read_text("utf-8")
    assert "HTTPS_PROXY" not in settings and "198.51.100.7" not in settings  # nothing of the note applied
    assert TOKEN not in settings and ROUTE not in settings and applied["token"] not in settings
    assert route_key(applied["token"]) not in settings and "127.0.0.1:5283" not in settings  # nothing applied again
    assert "latet agents apply" in words and "LATET_DATA_DIR" in words


def test_disconnect_deletes_nothing_through_a_linked_folder(applied, latet_server, tmp_path):
    elsewhere = tmp_path / "your-project"
    elsewhere.mkdir()
    (elsewhere / "your-project-settings.json").write_text("{}", "utf-8")
    sessions = data_dir() / "claude-settings"
    shutil.rmtree(sessions, ignore_errors=True)
    if not _junction(sessions, elsewhere):
        pytest.skip("this computer does not let a test make a link")
    run_cli("disconnect", "--yes")
    assert (elsewhere / "your-project-settings.json").exists()


def test_the_note_keeps_agent_ids_alone(applied, latet_server, tmp_path, monkeypatch):
    def stopped(self, config=None):
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(agents.OpenCode, "apply", stopped)
        run_cli("disconnect", "--yes")
    note = json.loads((connection_dir() / "disconnecting.json").read_text("utf-8"))
    revoked = note.get("revoked", [])  # C-M3: the copies a run revoked, by their SHA-256 alone
    assert set(note) <= {"agents", "revoked"} and all(re.fullmatch(r"[0-9a-f]{64}", item) for item in revoked)
    assert applied["token"] not in json.dumps(note)
    assert note["agents"] == sorted(note["agents"]) and set(note["agents"]) <= {"claude", "codex", "opencode"}


@pytest.mark.parametrize("command", [["agents", "revert"], ["global-uninstall"]], ids=["revert", "uninstall"])
def test_reverting_reads_nothing_of_a_connected_computers_folder_others_can_change(connected, claude_home, quiet,
                                                                                    command, monkeypatch):  # fmt: skip
    save_records({"claude": Record("claude", 0.0, [FileEdit(str(claude_home / "settings.json"), None, None, "x")], {})})
    manifest = data_dir() / "integrations.json"
    before = manifest.read_bytes()
    connection_path = data_dir() / "connection.yaml"
    connection_before = connection_path.read_bytes()
    for agent in agents.ALL.values():
        monkeypatch.setattr(type(agent), "revert", lambda *a, **kw: pytest.fail("agent undo reached"))
    _open_to_others(data_dir())
    with _no_unsafe_record_access(monkeypatch):
        done = run_cli(*command)
        with pytest.raises(RecordsUnavailable):
            load_records()
    assert done.exit_code == 1 and "Integration records unavailable" in done.output
    assert "LATET_DATA_DIR" in done.output and manifest.read_bytes() == before
    assert connection_path.read_bytes() == connection_before and not (claude_home / "settings.json").exists()


# --- an agent's start pays nothing for a connection when there is none (T5.19) ---------------------------------------


def test_round2_unsafe_disconnect_preserves_session_and_certificate_files(applied, latet_server):
    certificate = connection_dir() / "left.pem"
    certificate.write_text("certificate", "utf-8")
    _open_to_others(data_dir())
    done = run_cli("disconnect", "--yes")
    assert applied["crashed"].exists() and certificate.exists()
    assert done.exit_code == 1 and str(applied["crashed"].parent) in " ".join(done.output.split())
    assert load_connection(unchecked=True) is not None


@pytest.mark.parametrize("connection_state", ["different", "missing"])
@pytest.mark.parametrize("keep_settings_token", [True, False])
def test_round2_unsafe_disconnect_cleans_unknown_space_and_quarantines_records(
    applied, latet_server, connection_state, keep_settings_token, monkeypatch
):
    applied["crashed"].unlink()
    settings_path = applied["files"][0]
    settings = json.loads(settings_path.read_text("utf-8"))
    if keep_settings_token:
        settings["env"]["ROUTE_ONLY"] = route_key(applied["token"])
        settings["env"]["LATET_BRIDGE_TOKEN"] = applied["token"]  # as Windows writes it (R5: never elsewhere)
    else:
        settings["env"].pop("LATET_BRIDGE_TOKEN", None)
    settings_path.write_text(json.dumps(settings), "utf-8")
    if connection_state == "different":
        connection = applied["connection"]
        save_connection(connection.model_copy(update={"url": "https://another.acme.lan"}))
    else:
        (data_dir() / "connection.yaml").unlink()
    manifest = data_dir() / "integrations.json"
    manifest_before = manifest.read_bytes()
    connection_path = data_dir() / "connection.yaml"
    connection_before = connection_path.read_bytes() if connection_path.exists() else None
    _open_to_others(data_dir())
    with _no_unsafe_record_access(monkeypatch):
        done = run_cli("disconnect", "--yes")
    for path in applied["files"][:3]:
        assert route_key(applied["token"]) not in path.read_text("utf-8")
    words = " ".join(done.output.split())
    if os.name == "nt":
        assert done.exit_code == 1 and "integration records retained" in words
        assert manifest.read_bytes() == manifest_before and not manifest.with_suffix(".json.untrusted").exists()
        assert not (data_dir() / "backups.untrusted").exists()
        if connection_before is None:
            assert not connection_path.exists()
        else:
            assert connection_path.read_bytes() == connection_before
    else:
        assert not manifest.exists() and manifest.with_suffix(".json.untrusted").read_bytes() == manifest_before
    assert "new data folder of your own" in words and "icacls" not in words


def test_round2_check_recognizes_an_unknown_spaces_shape(computer, claude_home):
    from latet.cli.connect import _still_holding

    path = claude_home / "settings.json"
    path.write_text(json.dumps({"env": {"URL": f"https://other.acme.lan/u/{ROUTE}/anthropic"}}))
    assert (path, "the address of your space on the server") in _still_holding([], [], [])


def test_round2_codex_session_refuses_linked_home(connected, tmp_path):
    home, target = tmp_path / "linked-codex", tmp_path / "shared-codex"
    target.mkdir()
    if not _junction(home, target):
        pytest.skip("this computer does not let a test make a link")
    with pytest.raises(wrappers.CodexArgs, match="link"):
        wrappers.codex_session_profile({"openai_base_url": SPACE}, None, home)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("command", ["app-server", "cloud-tasks", "execpolicy"])
def test_round2_codex_refuses_subcommands_before_contacting_server(connected, monkeypatch, command, capsys):
    from latet.cli.main import _codex

    monkeypatch.setattr("latet.cli.main._require", lambda exe: None)
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda: pytest.fail("server contacted"))
    assert _codex([command]) == 2
    assert "may go directly to its providers" in capsys.readouterr().err


def test_round2_codex_preserves_separator(connected, monkeypatch, tmp_path):
    monkeypatch.setattr(wrappers, "codex_version", lambda exe: (0, 162, 0))
    args = ["exec", "-i", "a.png", "b.png", "-p", "work", "--", "hi"]
    argv, _ = wrappers.codex_argv(args, {}, profile_file=tmp_path / "latet-0123456789abcdef.config.toml")
    assert argv[-2:] == ["--", "hi"]


def test_round2_untrusted_backups_cannot_be_read_as_undo_records(applied, latet_server, monkeypatch):
    applied["crashed"].unlink()
    backup = data_dir() / "backups" / "old" / "integrations.json.backup"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text('{"previous":{"HTTPS_PROXY":"http://evil.invalid"}}')
    before = backup.read_bytes()
    manifest = data_dir() / "integrations.json"
    manifest_before = manifest.read_bytes()
    connection_path = data_dir() / "connection.yaml"
    connection_before = connection_path.read_bytes()
    _open_to_others(data_dir())
    with _no_unsafe_record_access(monkeypatch):
        done = run_cli("disconnect", "--yes")
    if os.name == "nt":
        assert done.exit_code == 1 and "integration records retained" in done.output
        assert backup.read_bytes() == before and not (data_dir() / "backups.untrusted").exists()
        assert manifest.read_bytes() == manifest_before and not manifest.with_suffix(".json.untrusted").exists()
        assert connection_path.read_bytes() == connection_before
    else:
        assert not backup.exists()
        assert (data_dir() / "backups.untrusted" / "old" / backup.name).read_bytes() == before


def test_round2_partial_revocation_never_says_both_stay_valid(applied, latet_server, monkeypatch):
    from types import SimpleNamespace

    connection = applied["connection"]
    copies = [
        SimpleNamespace(
            token=applied["token"],
            url=connection.url,
            fingerprint=connection.fingerprint,
            user=connection.user,
            is_for=lambda other: True,
        ),
        SimpleNamespace(
            token="latet_pat_" + "q" * 43,
            url=connection.url,
            fingerprint=connection.fingerprint,
            user=connection.user,
            is_for=lambda other: True,
        ),
    ]
    monkeypatch.setattr("latet.server.connection.token_copies", lambda folder: (copies, None))
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 0, done.output
    assert applied["token"] not in latet_server.tokens
    assert "a copy not revoked may remain valid" in done.output
    assert "both stay valid" not in done.output
    assert "no token was revoked in this run" not in done.output


def test_round2_regular_reader_names_dangling_links(computer, tmp_path):
    from latet.cli.connect import _read_regular

    path = tmp_path / "dangling"
    try:
        path.symlink_to(tmp_path / "absent")
    except OSError:
        pytest.skip("this computer does not let a test make a symlink")
    with pytest.raises(OSError, match="link"):
        _read_regular(path)


def test_round2_regular_reader_names_a_folder(computer, tmp_path):
    from latet.cli.connect import _read_regular

    with pytest.raises(OSError, match="folder"):
        _read_regular(tmp_path)


def test_round2_final_check_explains_a_folder_in_place_of_settings(computer, claude_home):
    from latet.cli.connect import _still_holding

    path = claude_home / "settings.json"
    path.mkdir()
    assert any(found == path and "folder" in why for found, why in _still_holding([], [], []))


# Task 6 lifecycle characterizations. Descriptor assertions wait for reviewed EC8 interfaces.
@pytest.fixture
def trusted_helper_install(monkeypatch):
    """Declare only this test's code roots trusted; all private state and OS-root admission stays real."""
    from latet import claude_bridge
    from latet.integrations import base as checked_files

    declared = {
        Path(sys.executable).resolve(): False,
        Path(sys.prefix).resolve(): True,
        Path(sys.base_prefix).resolve(): True,
        Path(claude_bridge.__file__).parent.resolve(): True,
    }
    install = claude_bridge._install_path

    def admitted(path, *, directory, **what):
        resolved = path.resolve(strict=True)
        if resolved not in declared:
            return install(path, directory=directory, **what)
        assert declared[resolved] is directory
        found = checked_files._plain_path(resolved, directory=directory)
        assert stat.S_ISDIR(found.st_mode) if directory else stat.S_ISREG(found.st_mode)
        return resolved

    monkeypatch.setattr(claude_bridge, "_install_path", admitted)


@WINDOWS
def test_task6_real_foreign_writable_install_is_refused_before_apply_effects(connected, claude_home, monkeypatch):
    from latet.integrations import base as checked_files
    from latet.server import connection as selected_connection
    from latet.storage import folders

    executable = Path(sys.executable).resolve(strict=True)
    owner, grants = folders.folder_grants(executable)
    trusted = {folders.my_sid(), folders.SYSTEM, folders.ADMINISTRATORS, folders.TRUSTED_INSTALLER}
    unsafe = owner not in trusted or any(
        not grant.inherit_only
        and grant.sid not in trusted
        and grant.sid != folders.OWNER_RIGHTS
        and not folders._app_grant(grant.sid)
        and grant.mask & (~0xA01200A9 & 0xFFFFFFFF)
        for grant in grants
    )
    if not unsafe:
        pytest.skip("current installation lacks the observed foreign-write policy condition")
    settings = claude_home / "settings.json"
    settings.write_text(json.dumps({"env": {"TASK6_PERSONAL_SETTING": "keep"}}), "utf-8")
    assert load_connection() is not None and agents._token_kept_out(settings) is None
    checked_files.records_preflight(write=True)
    root = data_dir() / "connection" / "claude-helper"
    selected_connection._prepare_output(root, "owned test output admission")
    preserved = {
        path: path.read_bytes()
        for path in (selected_connection.connection_path(), data_dir() / "server.token", settings)
    }

    def forbidden(*args, **kwargs):
        pytest.fail("setup effects ran after unsafe installation admission")

    for name in ("backup", "write_atomic", "save_records"):
        monkeypatch.setattr(agents, name, forbidden)
    monkeypatch.setattr(agents.ClaudeCode, "prepare_marketplace", forbidden)
    monkeypatch.setattr(agents.ClaudeCode, "_claude", forbidden)
    with pytest.raises(NotConnected, match="helper binding"):
        agents.ClaudeCode().apply()
    assert all(path.read_bytes() == raw for path, raw in preserved.items())
    assert not root.exists() and checked_files.load_records() == {}


@WINDOWS
def test_task6_reapply_tracks_a_changed_personal_token_without_disclosing_it(
    connected, claude_home, trusted_helper_install
):
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    replacement = "latet_pat_" + "y" * 43
    connection = load_connection()
    assert connection is not None
    save_server_token(data_dir(), connection, replacement)
    claude.apply(agents.ClaudeOptions())
    env = _settings_env(claude_home)
    expected = f"{URL}/u/{route_key(replacement)}"
    assert env["LATET_BRIDGE_TOKEN"] == replacement
    assert env["LATET_BRIDGE_URL"] == expected and env["ANTHROPIC_BASE_URL"] == expected + "/anthropic"
    diagnostics = " ".join(claude.said)
    assert TOKEN not in diagnostics and replacement not in diagnostics
    assert ROUTE not in diagnostics and route_key(replacement) not in diagnostics


@WINDOWS
def test_task6_reapply_then_revert_preserves_unrelated_settings_and_removes_remote_authority(
    connected, claude_home, trusted_helper_install
):
    path = claude_home / "settings.json"
    path.write_text(json.dumps({"env": {"TASK6_PERSONAL_SETTING": "keep"}}), "utf-8")
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    claude.apply(agents.ClaudeOptions())
    assert _settings_env(claude_home)["LATET_BRIDGE_TOKEN"] == TOKEN
    claude.revert()
    restored = path.read_text("utf-8")
    assert _settings_env(claude_home) == {"TASK6_PERSONAL_SETTING": "keep"}
    assert TOKEN not in restored and ROUTE not in restored
    assert "claude" not in load_records()


def test_round2_regular_reader_names_a_dangling_junction(computer, tmp_path):
    from latet.cli.connect import _read_regular

    target, link = tmp_path / "target", tmp_path / "junction"
    target.mkdir()
    if not _junction(link, target):
        pytest.skip("this computer does not let a test make a link")
    target.rmdir()
    with pytest.raises(OSError, match="link"):
        _read_regular(link)


def test_round2_final_check_names_a_profile_in_a_linked_codex_home(computer, tmp_path, monkeypatch):
    from latet.cli.connect import _still_holding

    target, home = tmp_path / "shared-codex", tmp_path / "codex-link"
    target.mkdir()
    if not _junction(home, target):
        pytest.skip("this computer does not let a test make a link")
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "latet-0123456789abcdef.config.toml"
    path.write_text(f'openai_base_url = "{SPACE}/openai/v1"')
    assert (path, "the address of your space on the server") in _still_holding([], [], [])


def test_round2_unsafe_disconnect_never_traverses_retained_directories(applied, latet_server, monkeypatch):
    retained = {data_dir() / "claude-settings", connection_dir()}
    original = Path.rglob

    def guarded(path, *args, **kwargs):
        assert path not in retained, "traversed an attacker-swappable directory"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", guarded)
    _open_to_others(data_dir())
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 1 and done.exception is not None
    assert not isinstance(done.exception, AssertionError)
    words = " ".join(done.output.split())
    assert all(str(path) in words for path in retained)
    assert load_connection(unchecked=True) is not None


def test_an_agents_start_loads_nothing_of_a_server_when_this_computer_is_not_connected(tmp_path):
    code = "\n".join(
        [
            "import sys",
            "import latet.cli.main",
            "from latet import wrappers",
            "from latet.config import load_config",
            "from latet.integrations import agents",
            "wrappers.claude_env(); wrappers.opencode_env(); wrappers.generic_env(); wrappers.server_base()",
            "wrappers.extra_ca_env('codex'); wrappers.untrusting('codex'); wrappers.daemon_token()",
            "agents.ClaudeCode().route(load_config())",
            "print(sorted(name for name in ('httpx', 'truststore', 'latet.engine.reviewer') if name in sys.modules))",
        ]
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LATET_", "CLAUDE", "CODEX"))}
    env |= {"LATET_DATA_DIR": str(tmp_path / "data"), "LATET_CONFIG": str(tmp_path / "config.yaml")}
    env |= {"LATET_KEYRING": "off", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"), "CODEX_HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
