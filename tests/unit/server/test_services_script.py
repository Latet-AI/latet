"""scripts/server-services.py: PostgreSQL and Valkey in Docker for the server's tests, on this computer only."""

import base64
import importlib.util
import ipaddress
import os
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ..test_gen_notices import gen

_SPEC = importlib.util.spec_from_file_location(
    "server_services", Path(__file__).resolve().parents[3] / "scripts" / "server-services.py"
)
assert _SPEC is not None and _SPEC.loader is not None
services = importlib.util.module_from_spec(_SPEC)
sys.modules["server_services"] = services
_SPEC.loader.exec_module(services)
# What only POSIX has: file modes and owners, and links any user may make (Windows asks a privilege for a link).
posix = pytest.mark.skipif(os.name == "nt", reason="file modes, owners and links are POSIX's")


@pytest.fixture(autouse=True)
def _tls_folder(monkeypatch, tmp_path):
    """The root certificate of the TLS PostgreSQL goes to the test's own folder."""
    monkeypatch.setattr(services, "TLS_FOLDER", tmp_path / "latet-test-tls")


def _tls():
    return services.certificates()


def test_the_services_listen_on_this_computer_only():
    for name in ("postgres", "valkey", "postgres-tls"):
        argv = services.docker_run(name, tls=_tls() if name == "postgres-tls" else None)
        ports = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
        assert ports and all(port.startswith("127.0.0.1:") for port in ports), argv
    assert services.DATABASE_URL.startswith("postgresql://latet:") and "@127.0.0.1:55432/" in services.DATABASE_URL
    assert services.VALKEY_URL == "redis://:latet-tests-only@127.0.0.1:56379/0"  # with a password, as a server's is
    assert services.tls_database_url().startswith("postgresql://latet:latet-tests-only@127.0.0.1:55433/latet?")


def test_the_images_are_pinned():
    assert services.IMAGES == {
        "postgres": "postgres:18-alpine", "valkey": "valkey/valkey:9.1-alpine", "postgres-tls": "postgres:18-alpine",
    }  # fmt: skip


def test_each_service_asks_for_the_password_of_its_address():
    password = urlsplit(services.VALKEY_URL).password
    assert password and password == urlsplit(services.DATABASE_URL).password
    assert password == urlsplit(services.tls_database_url()).password
    valkey = services.docker_run("valkey")
    assert valkey[valkey.index("--requirepass") + 1] == password
    assert f"POSTGRES_PASSWORD={password}" in services.docker_run("postgres")
    assert f"POSTGRES_PASSWORD={password}" in services.docker_run("postgres-tls", tls=_tls())


def test_the_tls_postgresql_is_verified_as_a_server_verifies_one():
    """Its address asks verify-full against a root certificate made for the tests alone, written where the address
    says, which signed the server's own certificate for 127.0.0.1. The server's key is written on no disk of this
    computer's: it goes, in base64, on docker's command line and into the container's environment (whoever reads the
    process list or `docker inspect` reads it: a throwaway key, for a test database with a public password), and the
    container's start unsets it before PostgreSQL runs."""
    certificate, key = services.certificates()
    root = x509.load_pem_x509_certificate((services.TLS_FOLDER / "root.crt").read_bytes())
    leaf = x509.load_pem_x509_certificate(certificate.encode())
    leaf.verify_directly_issued_by(root)
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("127.0.0.1") in names.get_values_for_type(x509.IPAddress)
    assert serialization.load_pem_private_key(key.encode(), None).public_key() == leaf.public_key()
    assert sorted(p.name for p in services.TLS_FOLDER.iterdir()) == ["root.crt"]  # no key left on the disk
    query = parse_qs(urlsplit(services.tls_database_url()).query)
    assert query == {"sslmode": ["verify-full"], "sslrootcert": [str(services.TLS_FOLDER / "root.crt")]}
    argv = services.docker_run("postgres-tls", tls=(certificate, key))
    assert f"LATET_TLS_KEY={base64.b64encode(key.encode()).decode()}" in argv
    start = argv[-1]
    assert "ssl=on" in start and "unset LATET_TLS_CERT LATET_TLS_KEY" in start
    assert start.index("unset LATET_TLS_CERT LATET_TLS_KEY") < start.index("exec docker-entrypoint.sh")


def test_the_root_is_kept_in_a_folder_of_the_users_own_never_the_shared_temp_folder(monkeypatch):
    """Another user of the computer can make a folder of the shared temp folder first, and plant a link in it: the
    root the TLS tests trust is kept under the user's own folders (%LOCALAPPDATA% on Windows, ~/.cache elsewhere)."""
    spec = importlib.util.spec_from_file_location("server_services_case", services.__file__)
    assert spec is not None and spec.loader is not None
    current = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patched:
        patched.setitem(sys.modules, spec.name, current)
        spec.loader.exec_module(current)
    temp = Path(tempfile.gettempdir()).resolve()
    assert not current.TLS_FOLDER.resolve().is_relative_to(temp)
    mine = Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path.home() / ".cache"
    assert current.TLS_FOLDER.parent == mine


@posix
def test_the_folder_of_the_root_is_made_for_the_user_alone():
    services.certificates()
    assert stat.S_IMODE(services.TLS_FOLDER.stat().st_mode) == 0o700


@posix
def test_a_link_planted_where_the_root_goes_is_replaced_and_what_it_led_to_left_as_it_was(tmp_path):
    services.TLS_FOLDER.mkdir(mode=0o700)
    target = tmp_path / "precious.txt"
    target.write_text("as it was")
    (services.TLS_FOLDER / "root.crt").symlink_to(target)
    services.certificates()
    assert target.read_text() == "as it was"
    written = services.TLS_FOLDER / "root.crt"
    assert not written.is_symlink() and written.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")


@posix
@pytest.mark.parametrize("mode", [0o777, 0o770, 0o702])
def test_a_folder_others_can_write_to_is_refused_saying_so(mode):
    services.TLS_FOLDER.mkdir()
    services.TLS_FOLDER.chmod(mode)
    with pytest.raises(SystemExit) as refused:
        services.certificates()
    assert "others can write to it" in str(refused.value.code) and str(services.TLS_FOLDER) in str(refused.value.code)
    assert not (services.TLS_FOLDER / "root.crt").exists()


@posix
def test_a_folder_of_another_user_or_a_link_in_its_place_is_refused_saying_so(monkeypatch, tmp_path):
    services.TLS_FOLDER.mkdir(mode=0o700)
    with monkeypatch.context() as patched:
        uid = os.getuid()
        patched.setattr(services.os, "getuid", lambda: uid + 1)  # the folder is someone else's
        with pytest.raises(SystemExit) as refused:
            services.certificates()
    assert "another user's" in str(refused.value.code)
    services.TLS_FOLDER.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    services.TLS_FOLDER.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SystemExit) as refused:
        services.certificates()
    assert "is a link" in str(refused.value.code) and not (elsewhere / "root.crt").exists()


def test_up_starts_the_tls_postgresql_alone_when_asked_and_env_says_its_address(monkeypatch, capsys):
    calls = fake_docker(monkeypatch)
    services.up(["postgres-tls"])
    runs = [argv for argv in calls if argv[:2] == ["docker", "run"]]
    assert len(runs) == 1 and "latet-test-postgres-tls" in runs[0] and (services.TLS_FOLDER / "root.crt").exists()
    capsys.readouterr()
    services.env(["postgres-tls"])
    assert capsys.readouterr().out == f"LATET_TEST_TLS_DATABASE_URL={services.tls_database_url()}\n"


def _running(*names):
    """A docker whose `ps` answers that the containers ``names`` run, and none other; every other command succeeds."""

    def answer(argv):
        if argv[:2] == ["docker", "ps"]:
            asked = argv[-1].removeprefix("name=^").removesuffix("$")
            return 0, "4f1d2c3b5a69\n" if asked in names else "", ""
        return 0, "", ""

    return answer


def test_env_names_the_tls_postgresql_only_when_it_runs_with_its_root(monkeypatch, capsys):
    """After `up postgres valkey` alone, `env` with no names says nothing of a TLS PostgreSQL: its tests then skip,
    saying why, rather than fail on an address nothing answers at."""
    fake_docker(monkeypatch, _running("latet-test-postgres", "latet-test-valkey"))
    services.env()
    said = capsys.readouterr().out
    assert "LATET_TEST_DATABASE_URL=" in said and "LATET_TEST_TLS_DATABASE_URL" not in said
    fake_docker(monkeypatch, _running("latet-test-postgres-tls"))
    services.env()  # it runs, but the root it is verified with is not there
    assert "LATET_TEST_TLS_DATABASE_URL" not in capsys.readouterr().out
    services.certificates()
    services.env()
    assert f"LATET_TEST_TLS_DATABASE_URL={services.tls_database_url()}\n" in capsys.readouterr().out


def test_up_starts_the_tls_postgresql_again_when_the_root_it_is_verified_with_is_gone(monkeypatch, capsys):
    calls = fake_docker(monkeypatch, _running("latet-test-postgres-tls"))
    services.up(["postgres-tls"])  # it runs, its root removed meanwhile
    assert len([argv for argv in calls if argv[:2] == ["docker", "run"]]) == 1
    assert (services.TLS_FOLDER / "root.crt").exists() and "started again" in capsys.readouterr().out
    calls.clear()
    services.up(["postgres-tls"])  # it runs with its root: left as it is
    assert not [argv for argv in calls if argv[:2] == ["docker", "run"]]
    assert "runs already" in capsys.readouterr().out


def _succeeds(argv):
    return 0, "", ""


def fake_docker(monkeypatch, answer=_succeeds):
    """The script with a docker that answers from ``answer`` (the command asked -> exit code, output, error); the
    commands it was asked."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        code, out, err = answer(list(argv))
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, argv, out, err)
        return subprocess.CompletedProcess(argv, code, out, err)

    monkeypatch.setattr(services, "subprocess", types.SimpleNamespace(run=run))
    return calls


def test_a_container_is_removed_with_the_volume_docker_made_for_it(monkeypatch):
    calls = fake_docker(monkeypatch)  # nothing runs yet: up starts all three, down removes all three
    services.up()
    assert (services.TLS_FOLDER / "root.crt").exists()
    services.down()
    removals = [argv for argv in calls if argv[:2] == ["docker", "rm"]]
    assert [argv[-1] for argv in removals] == [
        "latet-test-postgres",
        "latet-test-valkey",
        "latet-test-postgres-tls",
    ] * 2
    assert all(argv[:4] == ["docker", "rm", "-f", "-v"] for argv in removals), removals
    assert not services.TLS_FOLDER.exists()  # its root certificate goes with it


@pytest.mark.parametrize("command", ["up", "down"])
def test_without_docker_the_script_says_so_and_shows_no_traceback(monkeypatch, command):
    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(services, "subprocess", types.SimpleNamespace(run=missing))
    with pytest.raises(SystemExit) as stopped:
        getattr(services, command)()
    assert "docker was not found" in str(stopped.value.code)


def test_dockers_own_error_is_what_the_script_says_when_the_engine_is_down(monkeypatch, capsys):
    engine = "failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine: is the daemon running?"
    calls = fake_docker(monkeypatch, lambda argv: (1, "", engine))
    for command in (services.up, services.down):
        with pytest.raises(SystemExit) as stopped:
            command()
        assert engine in str(stopped.value.code)
    assert len(calls) == 2  # each stopped at its first docker command
    assert "removed" not in capsys.readouterr().out  # down did not say the containers were gone


def test_a_docker_run_that_fails_stops_the_script(monkeypatch):
    fake_docker(monkeypatch, lambda argv: (125, "", "") if argv[1] == "run" else (0, "", ""))
    with pytest.raises(SystemExit) as stopped:
        services.up()
    assert "docker run failed" in str(stopped.value.code) and "125" in str(stopped.value.code)


def test_the_lgpl_drivers_say_how_they_are_used():
    for name in ("psycopg", "psycopg-binary", "psycopg-pool"):  # each of the three the server's extra installs
        said = "\n".join(gen._details([gen.Entry(name, "3.3.0", "LGPL-3.0-only", "https://psycopg.org")]))
        assert "separate, unmodified package" in said and "replace it" in said, name
    assert "- Use:" not in "\n".join(gen._details([gen.Entry("redis", "6.4.0", "MIT", "https://github.com/redis")]))
