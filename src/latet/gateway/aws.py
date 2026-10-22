"""AWS credentials for signing Bedrock's requests: an AWS profile's, read as Amazon's own tools read it,
or the ones `latet claude` hands over for its session (credentials that live only in its shell). Kept in
memory only: never written, never shown."""

from __future__ import annotations

import asyncio
import configparser
import hashlib
import hmac
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.gateway.sigv4 import AwsCredentials

if TYPE_CHECKING:
    from latet.state.protocols import AwsSessions

_MARGIN = 300  # credentials that end within five minutes are read again
_STATIC = 900  # long-term keys are read again every quarter of an hour: the file may change
_RETRY_AFTER = 5  # a failure is the answer for a few seconds, then Latet tries again
_TIMEOUT = 30  # the AWS CLI waiting for a login or an MFA code must not hold a request for long
# A profile that signs in through AWS (SSO, a role): only the AWS CLI reads it.
_SIGN_IN_KEYS = ("sso_session", "sso_start_url", "role_arn", "web_identity_token_file", "login_session")


class AwsError(Exception):
    """Why Latet cannot sign for AWS: words that say what to do, and a ``code`` for the dashboard."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def profile_tag(token: str, profile: str) -> str:
    """The key a signing route carries for an AWS profile. Only this Latet (its token) makes it, so no
    other program or user of this computer can have requests signed in your name."""
    return hmac.new(token.encode(), b"aws-profile\0" + profile.encode(), hashlib.sha256).hexdigest()[:32]


def cli_places() -> tuple[Path, ...]:
    """Where the AWS CLI installs itself, worked out when asked: a daemon started at login does not see the
    shell's PATH, where pip, pipx and snap put it."""
    if sys.platform == "win32":
        return (Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Amazon" / "AWSCLIV2" / "aws.exe",)
    return (
        Path("/usr/local/bin/aws"),
        Path("/opt/homebrew/bin/aws"),
        Path("/usr/bin/aws"),
        Path.home() / ".local" / "bin" / "aws",
        Path("/snap/bin/aws"),
    )


def cli_folders() -> str:
    """Those places as folders, your home as ~: for a message that says where Latet looked."""
    home = Path.home()

    def shown(folder: Path) -> str:
        return f"~/{folder.relative_to(home).as_posix()}" if folder.is_relative_to(home) else folder.as_posix()

    return ", ".join(shown(place.parent) for place in cli_places())


def find_aws_cli(name: str = "aws") -> str | None:
    found = shutil.which(name)
    return found or next((str(place) for place in cli_places() if place.is_file()), None)


def _from_process(text: str) -> AwsCredentials:
    """Credentials as `credential_process` prints them (and `aws configure export-credentials`)."""
    data = json.loads(text)
    ends = data.get("Expiration")
    expires = datetime.fromisoformat(ends.replace("Z", "+00:00")).timestamp() if ends else None
    return AwsCredentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("SessionToken"), expires)


def credentials_end(*values: str | None) -> float | None:
    """When credentials end, from the times a tool left in the shell's variables (aws-vault and Granted set
    AWS_CREDENTIAL_EXPIRATION or AWS_SESSION_EXPIRATION, as ISO 8601): the earliest of those that are times,
    in epoch seconds. A value that is not a time is ignored. A time with no zone is UTC, as AWS writes them."""
    ends = []
    for value in values:
        try:
            when = datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        ends.append((when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp())
    return min(ends, default=None)


class AwsCredentialStore:
    """Credentials by AWS profile, read when first needed and again before they end; and the sessions'."""

    def __init__(
        self,
        which: Callable[[str], str | None] = find_aws_cli,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
        home: Path | None = None,
        sessions: AwsSessions | None = None,
    ) -> None:
        self._which, self._run, self._clock, self._home = which, run, clock, home
        self._cache: dict[str, tuple[AwsCredentials, float]] = {}  # profile → credentials, good until
        self._failed: dict[str, tuple[AwsError, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if sessions is None:
            from latet.state.local import LocalAwsSessions  # here: latet.state.local imports this module

            sessions = LocalAwsSessions(clock)
        self.sessions: AwsSessions = sessions  # this process's memory on a computer, Valkey (sealed) on a server

    # --- profiles ---------------------------------------------------------------------------------

    async def profile(self, name: str) -> AwsCredentials:
        """A profile's credentials; read once at a time, however many requests ask together."""
        if (cached := self._cached(name)) is not None:
            return cached
        async with self._locks.setdefault(name, asyncio.Lock()):
            if (cached := self._cached(name)) is not None:
                return cached
            failed = self._failed.get(name)
            if failed is not None and self._clock() < failed[1]:
                raise failed[0]
            try:
                credentials = await asyncio.to_thread(self._load, name)
            except AwsError as error:
                self._failed[name] = (error, self._clock() + _RETRY_AFTER)
                raise
            self._failed.pop(name, None)
            until = credentials.expires - _MARGIN if credentials.expires else self._clock() + _STATIC
            self._cache[name] = (credentials, until)
            return credentials

    def invalidate(self, name: str) -> None:
        """Amazon found them stale: read them again on the next request."""
        self._cache.pop(name, None)

    def _cached(self, name: str) -> AwsCredentials | None:
        entry = self._cache.get(name)
        return entry[0] if entry is not None and self._clock() < entry[1] else None

    def _load(self, name: str) -> AwsCredentials:
        cli = self._which("aws")
        return self._export(cli, name) if cli else self._from_files(name)

    def _quiet(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """A command with no input and no window: a prompt (an MFA code) makes it time out, not hang."""
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return self._run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            creationflags=flags,
            check=False,
        )

    def _export(self, cli: str, name: str) -> AwsCredentials:
        argv = [cli, "configure", "export-credentials", "--profile", name, "--format", "process"]
        try:
            done = self._quiet(argv)
        except subprocess.TimeoutExpired:
            raise AwsError(
                "aws-login-expired",
                f"the AWS CLI waited for an answer (a login, an MFA code) for profile {name}: run "
                f"`aws sso login --profile {name}` in a terminal, then try again",
            ) from None
        except OSError as exc:
            raise AwsError("aws-cli-missing", f"the AWS CLI did not run ({type(exc).__name__})") from None
        if done.returncode != 0:
            raise self._failure(name, done.stderr or "")
        try:
            return _from_process(done.stdout)
        except (ValueError, KeyError, TypeError):
            raise AwsError("aws-no-credentials", f"the AWS CLI gave no credentials for profile {name}") from None

    @staticmethod
    def _failure(name: str, error: str) -> AwsError:
        said = error.lower()
        if any(word in said for word in ("sso", "expired", "token")):
            return AwsError(
                "aws-login-expired",
                f"your AWS login for profile {name} has expired: run `aws sso login --profile {name}`",
            )
        if "could not be found" in said or "not found" in said:
            return AwsError("aws-no-credentials", f"there is no AWS profile {name} on this computer")
        first = error.strip().splitlines()[0][:200] if error.strip() else "no reason given"
        return AwsError("aws-no-credentials", f"the AWS CLI could not read profile {name}: {first}")

    def _from_files(self, name: str) -> AwsCredentials:
        """Without the AWS CLI: the keys written in its files, or a profile's `credential_process`."""
        home = self._home or Path.home()
        credentials = home / ".aws" / "credentials"
        config = home / ".aws" / "config"
        if self._home is None:
            credentials = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or credentials)
            config = Path(os.environ.get("AWS_CONFIG_FILE") or config)
        keys = _section(credentials, name)
        settings = _section(config, name if name == "default" else f"profile {name}")
        for values in (keys, settings):
            if values.get("aws_access_key_id") and values.get("aws_secret_access_key"):
                token = values.get("aws_session_token") or None
                return AwsCredentials(values["aws_access_key_id"], values["aws_secret_access_key"], token)
        if process := settings.get("credential_process"):
            try:
                done = self._quiet(shlex.split(process, posix=os.name != "nt"))
                if done.returncode == 0:
                    return _from_process(done.stdout)
            except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
                pass
            raise AwsError("aws-no-credentials", f"the credential_process of AWS profile {name} gave no credentials")
        if any(key in settings for key in _SIGN_IN_KEYS):
            raise AwsError(
                "aws-cli-missing",
                f"AWS profile {name} signs in through AWS (SSO or a role), which Latet reads with the AWS CLI. "
                f"The AWS CLI is not on Latet's PATH nor in {cli_folders()}: install it "
                "(https://aws.amazon.com/cli/), or start Latet from a shell where `aws` runs",
            )
        raise AwsError("aws-no-credentials", f"there is no AWS profile {name} on this computer")

    # --- sessions ---------------------------------------------------------------------------------

    def add_session(self, credentials: AwsCredentials) -> str:
        """Credentials `latet claude` found only in its shell, for its session's route."""
        return self.sessions.add_session(credentials)

    def session(self, session: str) -> AwsCredentials:
        return self.sessions.session(session)

    def has_session(self, session: str) -> bool:
        """Whether a session's credentials are still here: not dropped, not past their end."""
        return self.sessions.has_session(session)

    def drop_session(self, session: str) -> bool:
        return self.sessions.drop_session(session)


def _section(path: Path, name: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text("utf-8"))
    except (OSError, configparser.Error, UnicodeDecodeError):
        return {}
    return dict(parser[name]) if parser.has_section(name) else {}
