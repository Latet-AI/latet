"""AWS credentials for Bedrock: read as Amazon's tools read them, kept in memory only, never shown."""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from latet.config import Config
from latet.gateway.aws import AwsCredentialStore, AwsError, find_aws_cli, profile_tag
from latet.gateway.sigv4 import AwsCredentials
from latet.integrations.routes import claude_sources, detect_claude

NOON = datetime(2026, 10, 8, 12, 0, tzinfo=UTC).timestamp()
EXPORT = {
    "Version": 1,
    "AccessKeyId": "ASIAEXAMPLE",
    "SecretAccessKey": "the-secret",
    "SessionToken": "the-token",
    "Expiration": "2026-10-08T13:00:00+00:00",
}


class FakeRun:
    """The AWS CLI (or a credential_process): its answers in turn, and the calls made to it."""

    def __init__(self, *answers: tuple[int, str]) -> None:
        self.answers = list(answers) or [(0, json.dumps(EXPORT))]
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code, text = self.answers[min(len(self.calls), len(self.answers)) - 1]
        return subprocess.CompletedProcess(argv, code, text if code == 0 else "", "" if code == 0 else text)


def _store(run, clock=lambda: NOON, cli="C:/Program Files/Amazon/AWSCLIV2/aws.exe", home=None):
    return AwsCredentialStore(which=lambda name: cli, run=run, clock=clock, home=home)


def test_a_profile_is_read_with_the_aws_cli_until_it_nearly_expires():
    now = [NOON]
    run = FakeRun()
    store = _store(run, clock=lambda: now[0])
    credentials = asyncio.run(store.profile("work"))
    assert (credentials.access_key, credentials.token) == ("ASIAEXAMPLE", "the-token")
    argv, options = run.calls[0]
    assert argv[1:] == ["configure", "export-credentials", "--profile", "work", "--format", "process"]
    assert options["stdin"] == subprocess.DEVNULL and options["timeout"] == 30
    asyncio.run(store.profile("work"))
    assert len(run.calls) == 1
    now[0] = NOON + 3600 - 200  # 200 s before they expire
    asyncio.run(store.profile("work"))
    assert len(run.calls) == 2


def test_requests_at_once_read_a_profile_once():
    run = FakeRun()
    store = _store(run)

    async def many():
        return await asyncio.gather(*(store.profile("work") for _ in range(10)))

    assert len({c.access_key for c in asyncio.run(many())}) == 1
    assert len(run.calls) == 1


def test_an_expired_aws_login_says_how_to_log_in_again():
    now = [NOON]
    run = FakeRun((255, "Error when retrieving token from sso: Token has expired and refresh failed"))
    store = _store(run, clock=lambda: now[0])
    with pytest.raises(AwsError) as error:
        asyncio.run(store.profile("work"))
    assert error.value.code == "aws-login-expired" and "aws sso login --profile work" in str(error.value)
    with pytest.raises(AwsError):  # tried again only after a few seconds
        asyncio.run(store.profile("work"))
    assert len(run.calls) == 1
    now[0] += 6
    with pytest.raises(AwsError):
        asyncio.run(store.profile("work"))
    assert len(run.calls) == 2


def test_a_cli_that_asks_for_input_does_not_hang(monkeypatch):
    def asks(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(AwsError) as error:
        asyncio.run(_store(asks).profile("work"))
    assert error.value.code == "aws-login-expired"


def test_without_the_aws_cli_static_keys_are_read_from_its_files(tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text(
        "[work]\naws_access_key_id = AKIDEXAMPLE\naws_secret_access_key = wJalr\n", "utf-8"
    )
    credentials = asyncio.run(_store(FakeRun(), cli=None, home=tmp_path).profile("work"))
    assert (credentials.access_key, credentials.token) == ("AKIDEXAMPLE", None)


def test_without_the_aws_cli_a_credential_process_is_run(tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[profile tool]\ncredential_process = mytool --get work\n", "utf-8")
    run = FakeRun()
    credentials = asyncio.run(_store(run, cli=None, home=tmp_path).profile("tool"))
    assert credentials.access_key == "ASIAEXAMPLE" and run.calls[0][0] == ["mytool", "--get", "work"]


def test_without_the_aws_cli_an_sso_profile_cannot_be_read(tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[profile work]\nsso_session = acme\n", "utf-8")
    with pytest.raises(AwsError) as error:
        asyncio.run(_store(FakeRun(), cli=None, home=tmp_path).profile("work"))
    assert error.value.code == "aws-cli-missing"
    with pytest.raises(AwsError) as error:
        asyncio.run(_store(FakeRun(), cli=None, home=tmp_path).profile("nobody"))
    assert error.value.code == "aws-no-credentials"


@pytest.fixture
def posix(tmp_path, monkeypatch):
    """A POSIX computer whose home is tmp_path, and no `aws` on the PATH the daemon started with. Only files
    under tmp_path exist: an AWS CLI in /usr/local/bin on the machine running the tests does not answer."""
    real_is_file = Path.is_file
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(Path, "is_file", lambda self, **kw: real_is_file(self, **kw) and self.is_relative_to(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: None)
    return tmp_path


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", "utf-8")
    path.chmod(0o755)


def test_the_aws_cli_is_found_where_pip_pipx_and_snap_put_it(posix):
    _make_executable(posix / ".local" / "bin" / "aws")
    assert find_aws_cli() == str(posix / ".local" / "bin" / "aws")  # the home is read when asked, not at import


def test_a_missing_aws_cli_says_where_latet_looked(posix):
    (posix / ".aws").mkdir()
    (posix / ".aws" / "config").write_text("[profile work]\nsso_session = acme\n", "utf-8")
    with pytest.raises(AwsError) as error:
        asyncio.run(_store(FakeRun(), cli=None, home=posix).profile("work"))
    said = str(error.value)
    assert error.value.code == "aws-cli-missing"
    for place in ("PATH", "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "~/.local/bin", "/snap/bin"):
        assert place in said
    assert "https://aws.amazon.com/cli/" in said and "from a shell where `aws` runs" in said


def test_on_windows_the_aws_cli_is_still_looked_for_in_program_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name, *args, **kwargs: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    cli = tmp_path / "Amazon" / "AWSCLIV2" / "aws.exe"
    cli.parent.mkdir(parents=True)
    cli.write_text("", "utf-8")
    assert find_aws_cli() == str(cli)


def test_a_session_s_credentials_live_in_memory_until_they_expire():
    now = [NOON]
    store = _store(FakeRun(), clock=lambda: now[0])
    session = store.add_session(AwsCredentials("AKIDEXAMPLE", "s", "t", expires=NOON + 60))
    assert len(session) == 32 and store.session(session).access_key == "AKIDEXAMPLE"
    now[0] += 61
    with pytest.raises(AwsError) as error:
        store.session(session)
    assert error.value.code == "aws-session-gone"
    other = store.add_session(AwsCredentials("AKIDEXAMPLE", "s"))
    assert store.drop_session(other) and not store.drop_session(other)


def test_credentials_from_the_shell_end_at_the_time_the_shell_gave(tmp_path):
    now = [NOON]
    store = _store(FakeRun(), clock=lambda: now[0])
    shell = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "s",
        "AWS_SESSION_EXPIRATION": "2026-10-08T12:30:00Z",
    }

    def session_for(variables):  # what `latet claude` does: the shell's variables, then the daemon's store
        sources = claude_sources(variables, tmp_path, [])
        route = detect_claude(sources, Config(), "token", lambda url: "unused", store.add_session)
        return route.env["ANTHROPIC_BEDROCK_BASE_URL"].rsplit("/", 1)[1]

    ends = session_for(shell)
    no_time = session_for({**shell, "AWS_SESSION_EXPIRATION": "next week"})
    now[0] = NOON + 30 * 60 - 1
    assert store.session(ends).access_key == "AKIDEXAMPLE"
    now[0] = NOON + 30 * 60
    with pytest.raises(AwsError) as error:
        store.session(ends)
    assert error.value.code == "aws-session-gone"
    now[0] = NOON + 365 * 24 * 3600  # a value that is no time gives no end: held until it is dropped
    assert store.session(no_time).access_key == "AKIDEXAMPLE"


def test_a_session_is_held_until_it_is_dropped_or_its_credentials_end():
    now = [NOON]
    store = _store(FakeRun(), clock=lambda: now[0])
    short = store.add_session(AwsCredentials("AKIDEXAMPLE", "s", "t", expires=NOON + 60))
    long = store.add_session(AwsCredentials("AKIDEXAMPLE", "s"))
    assert store.has_session(short) and store.has_session(long) and not store.has_session("f" * 32)
    now[0] += 61
    assert not store.has_session(short) and store.has_session(long)
    store.drop_session(long)
    assert not store.has_session(long)


def test_a_profile_s_key_belongs_to_this_latet():
    key = profile_tag("token", "work")
    assert len(key) == 32 and key == profile_tag("token", "work")
    assert key != profile_tag("another token", "work") and key != profile_tag("token", "home")
