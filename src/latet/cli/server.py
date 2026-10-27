"""``latet server``: Latet on a server. On the server's machine, `init`, `migrate`, `run`, `ready`, `certificate` and
`service install` (they need the `server` extra, but `certificate`); from any computer with an administration token,
everything else, over HTTPS (``admin_call``).

The commands that open the server's database or serve it (`init`, `migrate`, `run`) set the server's log up first,
before anything can log, then again as its settings say: a library's record while a pool opens meets the server's gate
and redaction, never an unconfigured standard error. What stops them is said in its fixed words (E0b's errors, the
settings' refusals), never a driver's, with exit 2."""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from latet.cli.main import app
from latet.log_paging import ID_MOST, LogCursor, LogFilters, finite_time
from latet.server.tokens import OUTLIVES_ISSUER

if TYPE_CHECKING:
    from latet.config import Config
    from latet.server.client import ReceiveBudget

# The `latet server` group. Later parts add theirs: E3 and E4 import it by this name, E1, E2 and E5 find it by its
# name in `app`. (What `latet server run` serves is latet.server.operations.server_app, another module's.)
server_app = typer.Typer(
    help="Latet on a server: one Latet for the computers and programs of a team or a company.", no_args_is_help=True
)
app.add_typer(server_app, name="server")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", help="The server's settings file (server.yaml); its LATET_SERVER_* variables win over it."
    ),
]
# Which server the administration asks, and with which token: the group's options (the plan's Ruling 5).
_target: dict[str, Any] = {"server": None, "fingerprint": None, "token_file": None}
# What a connected computer gives each agent to trust the server's own certificate (E0c's spike: all three take it).
_AGENTS_TRUST = "NODE_EXTRA_CA_CERTS to Claude Code and OpenCode, CODEX_CA_CERTIFICATE to Codex"


@server_app.callback()
def _options(
    server: Annotated[
        str | None,
        typer.Option("--server", help="The server's address (default: LATET_SERVER_URL, or this computer's)."),
    ] = None,
    fingerprint: Annotated[
        str | None,
        typer.Option(
            "--fingerprint", help="Its self-signed certificate's fingerprint (default: LATET_SERVER_FINGERPRINT)."
        ),
    ] = None,
    admin_token_file: Annotated[
        Path | None,
        typer.Option(
            "--admin-token-file",
            help="A file holding an administration token (or the token in LATET_SERVER_ADMIN_TOKEN, bound to "
            "LATET_SERVER_URL).",
        ),
    ] = None,
) -> None:
    if admin_token_file is not None:
        _not_secret(str(admin_token_file))
    _target.update(server=server, fingerprint=fingerprint, token_file=admin_token_file)


def _local(config: Path | None) -> Any:
    """The server's settings, on its own machine: without the `server` extra, or with settings that cannot be read,
    said, and the command ended (exit 2)."""
    from latet.server import instance

    try:
        instance.require_extra()
        return instance.settings(config)
    except instance.NeedsExtra as exc:
        typer.secho(f"{exc}.", fg="red", err=True)
        raise typer.Exit(2) from None
    except (ValueError, OSError) as exc:  # a plain ValueError of the settings' words: pydantic's own is never raised
        typer.secho(f"The server's settings cannot be read: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None


def _logging_first() -> None:
    """The server's log, set up before anything can log; set up again as the settings say once they are read."""
    from latet.server.logs import configure_logging

    configure_logging("text", "info")


def _done[T](stopped: str, work: Callable[[], T]) -> T:
    """``work()``; what stops it said in its fixed words, and the command ended (exit 2)."""
    try:
        return work()
    except (ValueError, OSError, RuntimeError) as exc:  # E0b's start errors, the master key's, a folder refused
        typer.secho(f"{stopped}: {exc}", fg="red", err=True)
        raise typer.Exit(2) from None


def _logging_as(settings: Any) -> None:
    from latet.server.logs import configure_logging

    configure_logging(settings.log_format, settings.log_level)


@server_app.command("init")
def server_init(
    config: ConfigOption = None,
    new_admin_token: Annotated[
        bool, typer.Option("--new-admin-token", help="Issue another administration token, when yours is lost.")
    ] = False,
    days: Annotated[
        int | None,
        typer.Option(min=1, max=3650, help="The token issued lasts this many days (default: admin_token_days)."),
    ] = None,
    never: Annotated[
        bool, typer.Option("--never", help="The token issued never expires: keep it apart, used only to issue others.")
    ] = False,
) -> None:
    """Prepare a Latet server: its master key, the database's tables, and the first administration token.

    Run it once, on a machine with the server's settings and its database's access. The master key is written to the
    file master_key_file names when it is not there (in a folder that is yours alone); when nothing names one, a new key
    is printed for you to keep. A token issues tokens that end no later than itself: `--never` here is how a token that
    does not expire is made.
    """
    from latet.cli.agent import agent_name
    from latet.server import instance

    # Before the settings are read and before anything prints a secret: a new master key, an administration token.
    if agent_name() is not None:
        _stop(
            "An agent cannot initialize a server: it would be shown the master key or an administration token. Run "
            "`latet server init` yourself in a terminal of your own."
        )
    _logging_first()
    settings = _local(config)
    if days is not None and never:
        raise typer.BadParameter("a token lasts some days or for good: --days or --never", param_hint="--never")

    def master_key() -> Path | str | None:
        _logging_as(settings)
        return instance.ensure_master_key(settings)

    made = _done("Not initialized", master_key)
    if isinstance(made, str):
        typer.echo("No master key: it seals every identity's key in the database. A new one, shown once:")
        typer.echo(f"  {made}")
        typer.echo("Keep it in a private file named by master_key_file (LATET_MASTER_KEY_FILE), or in")
        typer.echo("LATET_MASTER_KEY, with a copy apart from the database's backups; then run")
        typer.echo("`latet server init` again.")
        raise typer.Exit(2)
    if made is not None:
        typer.echo(f"A new master key is in {made}: keep a copy apart from the database's backups. Without it, no")
        typer.echo("identity's vault can be read again.")
    applied = _done("Not initialized", lambda: instance.migrate_schema(settings))
    plural = "s" if len(applied) != 1 else ""
    typer.echo(f"The database: {len(applied)} migration{plural} applied." if applied else "The database is up to date.")
    from latet.server.app import open_server

    server = _done("Not initialized", lambda: open_server(settings))
    try:
        issued = _done(
            "Not initialized",
            lambda: instance.first_admin_token(server, again=new_admin_token, days=days or 0, never=never),
        )
    finally:
        server.close()
    if issued is None and (days is not None or never):  # no token is made: what they ask would go unsaid
        said = "An administration token was issued before, and none is made: --days and --never apply with"
        typer.secho(f"{said} --new-admin-token, which issues another here.", fg="red", err=True)
        raise typer.Exit(2)
    if issued is None:
        typer.echo("An administration token was issued before: `latet server admin-token issue` makes another with it,")
        typer.echo("or `latet server init --new-admin-token` here, when it is lost.")
        return
    token_id, secret = issued
    typer.echo(f"An administration token ({token_id}), shown once: keep it in your password manager.")
    typer.echo(f"  {secret}")
    if never:
        typer.echo("It does not expire: keep it apart, used only to issue the tokens you use from day to day")
        typer.echo("(`latet server admin-token issue`), each of which ends no later than the token that issues it.")
    else:
        named = "" if days else " (admin_token_days)"
        typer.echo(f"It expires in {days or settings.admin_token_days} days{named}, and every token it issues")
        typer.echo("ends no later than it: `latet server admin-token issue` makes the next ones, and")
        typer.echo("`latet server admin-token list` shows them all. A token that does not expire comes from")
        typer.echo("`latet server init --new-admin-token --never` here: keep it apart, used only to issue others.")
    typer.echo("Start the server: latet server run. Then, from any computer, with that token:")
    typer.echo(f"  latet server --server {settings.url} --admin-token-file <file> status")
    typer.echo("Or omit --admin-token-file to give the token at its hidden prompt in your own terminal.")


@server_app.command("migrate")
def server_migrate(
    config: ConfigOption = None,
    check: Annotated[bool, typer.Option("--check", help="Only say whether the tables are this version's.")] = False,
) -> None:
    """Bring the database's tables to this version of Latet (one machine at a time: the others wait for it)."""
    from latet.server import instance

    _logging_first()
    settings = _local(config)
    if check:

        def checked() -> str | None:
            _logging_as(settings)
            return instance.schema_state(settings)

        why = _done("Not checked", checked)
        typer.echo("The database's tables are this version's." if why is None else why)
        raise typer.Exit(0 if why is None else 1)

    def migrated() -> list[str]:
        _logging_as(settings)
        return instance.migrate_schema(settings)

    applied = _done("Not migrated", migrated)
    if applied:
        typer.echo(f"Applied: {', '.join(applied)}.")
    # What the migrations leave, as an instance's start reads it: a schema newer than this Latet keeps the names of the
    # migrations it does not know, which migrating skips.
    why = _done("Not checked", lambda: instance.schema_state(settings))
    if why is not None:
        typer.echo(why)
        raise typer.Exit(1)
    if not applied:
        typer.echo("The database is up to date: nothing to apply.")
        return
    typer.echo("Start, or restart, every instance with this version of Latet.")


@server_app.command("run")
def server_run(config: ConfigOption = None) -> None:
    """Run an instance of the Latet server in the foreground: the container's command, and the services'."""
    from latet.server import instance

    if instance.asyncio_debug():  # before anything is opened: its lines would name callbacks with what they hold
        typer.secho(f"Not started: {instance.DEBUG_MODE}.", fg="red", err=True)
        raise typer.Exit(2)
    _logging_first()
    settings = _local(config)
    made = _done("Not started", lambda: instance.make_instance(settings))
    made.run()


@server_app.command("ready")
def server_ready(config: ConfigOption = None) -> None:
    """Whether this machine's instance answers ready (PostgreSQL and Valkey answer it): exit 0, else 1."""
    from latet.server import instance

    answered, why = instance.ready(_local(config))
    typer.echo(why)
    raise typer.Exit(0 if answered else 1)


service_app = typer.Typer(help="Run this machine's Latet server as an operating-system service.", no_args_is_help=True)
server_app.add_typer(service_app, name="service")


@service_app.command("install")
def server_service_install(
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", help="Install without a prompt when run by your own script.")] = False,
    print_only: Annotated[
        bool, typer.Option("--print", help="Show the service plan without changing anything.")
    ] = False,
) -> None:
    """Show and install a systemd unit or a Windows service for the machine's Latet.

    Install Latet outside personal home folders, and keep its settings in a trusted
    machine directory. The service is enabled at boot; you start it when ready.
    """
    from latet.cli.agent import agent_name

    # This precedes settings, metadata, privilege checks and all install effects,
    # including the --yes and --print paths, as for the administration's guard.
    if agent_name() is not None:
        _stop("An agent cannot install a service: run this command yourself in a terminal of your own.")

    from latet.config import in_container
    from latet.server import service

    kind = service.platform()
    if kind not in ("linux", "windows"):
        _stop("Service installation needs Linux or Windows.")
    if in_container():
        _stop("In a container, run `latet server run` as the container's command.")
    selected = config or Path(
        os.environ.get("LATET_SERVER_CONFIG")
        or (str(service.LINUX_CONFIG) if kind == "linux" else str(service.windows_folder() / "server.yaml"))
    )
    selected = selected.absolute()
    try:
        service._arguments([str(selected)])
        problem = service.installation_problem(kind, selected)
    except (OSError, ValueError, RuntimeError):
        _stop("The machine installation paths cannot be checked.")
    if problem:
        _stop(problem)
    if problem := service.configuration_problem(selected):
        _stop(problem)
    settings = _local(selected)
    for name in service.SECRET_SETTINGS:
        path = getattr(settings, name, None)
        if path is not None and service.secret_file_problem(Path(path), machine_service=True):
            _stop(f"{name} must name a private, trusted file readable by the service.")
    executable = [sys.executable, "-I", "-m"]
    base = None
    try:
        if kind == "linux":
            unit = service.systemd_unit([*executable, "latet", "server", "run", "--config", str(selected)])
            typer.echo(unit)
            steps = []
        else:
            base = service.windows_folder()
            steps = service.windows_commands([*executable, "latet.server.winservice", "--config", str(selected)], base)
            unit = ""
            for path, writable in ((base, False), (base / "data", True), (base / "logs", True)):
                typer.echo(f"Create or verify protected directory {path}")
                typer.echo(service.windows_sddl(writable=writable))
            for step in steps:
                typer.echo(service.subprocess.list2cmdline(step))
    except (OSError, ValueError, RuntimeError):
        _stop("The service plan could not be constructed.")
    if print_only:
        return
    if not service.privileged(kind):
        _stop("Run service installation as root on Linux, or as an administrator on Windows.")
    _confirm("Install this service?", yes)
    try:
        if kind == "linux":
            service.install_systemd(unit)
        else:
            service.install_windows(steps, base)
    except (OSError, ValueError, RuntimeError):
        _stop("The service could not be installed. Check its trusted paths, permissions and service manager.")
    typer.echo("The service is installed and enabled at boot.")
    typer.echo("Start it: systemctl start latet-server" if kind == "linux" else "Start it: Start-Service LatetServer")


def _address(url: str) -> str:
    from latet.server.connection import server_url

    try:
        return server_url(url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--url") from None


@server_app.command("certificate")
def server_certificate(
    file: Annotated[Path | None, typer.Argument(help="A certificate (PEM) to show.")] = None,
    make: Annotated[bool, typer.Option("--make", help="Make a self-signed certificate, for a first try.")] = False,
    url: Annotated[
        str | None, typer.Option(help="The server's address: what --make names; alone, the certificate it shows.")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="With --make: the folder for server.crt and server.key.")] = None,
    days: Annotated[int, typer.Option(min=1, max=825, help="With --make: how long it lasts, in days.")] = 825,
) -> None:
    """Show a certificate's fingerprint and expiry, or make a self-signed one for a first try.

    `--make --url https://latet.acme.lan --out certs` makes certs/server.crt and certs/server.key; a file shows that
    certificate; `--url` alone shows the one this computer sees at that address (a machine in between can change it:
    read the fingerprint people pin from the certificate's file on the server's machine). People connect with
    `latet connect <address> --fingerprint <its fingerprint>`, which asks for their one-time code.
    A certificate that is an authority is no server's own, and is refused: computers given it would trust whatever its
    key signs.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from latet.engine.reviewer import certificate as certificates
    from latet.server.certificate import AUTHORITY, make_certificate

    address = _address(url) if url is not None else None
    if make:
        if address is None:
            raise typer.BadParameter("--make needs --url, the address people use", param_hint="--url")
        named, folder = address, out or Path.cwd()
        crt, _, _ = _done("Not made", lambda: make_certificate(named, folder, days))
        pem, where = crt.read_text("utf-8"), str(crt)
    elif file is not None:
        try:  # each certificate it holds read whole: a text of two decodes to no certificate's fingerprint
            pem, where = file.read_text("utf-8"), str(file)
            x509.load_pem_x509_certificates(pem.encode())
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"not a certificate this can read: {exc}", param_hint="FILE") from None
    elif address is not None:
        try:
            pem, _ = certificates.fetch(address)
        except (OSError, ValueError) as exc:
            typer.secho(f"{address} could not be reached: {exc}", fg="red", err=True)
            raise typer.Exit(1) from None
        where = address
    else:
        said = "Which certificate? A file, --url <the server's address>, or --make --url <it> --out <folder>."
        typer.secho(said, fg="red", err=True)
        raise typer.Exit(2)
    if certificates.is_authority(pem):  # CA:TRUE, no CA:FALSE, or more than one: never one to pin or to give agents
        typer.secho(f"The certificate of {where} {AUTHORITY}.", fg="red", err=True)
        raise typer.Exit(2)
    # The one certificate the text holds, alone: a file may hold a byte-order mark, a tool's attributes or the key
    # around it, which neither the fingerprint nor anything printed takes.
    pem = x509.load_pem_x509_certificates(pem.encode())[0].public_bytes(serialization.Encoding.PEM).decode()
    fingerprint = certificates.fingerprint_of(pem)
    typer.echo(f"The certificate of {where}:")
    typer.echo(f"  {fingerprint}")
    typer.echo(f"  SHA-256 {certificates.readable(fingerprint)}")
    typer.echo(f"  expires {certificates.expires(pem).date().isoformat()}")
    if file is None and not make:  # what a server presents, as it reached this computer
        typer.echo("This is what this computer sees at that address: a machine in between can put another certificate")
        typer.echo("in its place. Read the fingerprint people pin from the certificate's file on the server's machine:")
        typer.echo("latet server certificate <file>.")
    if make:
        typer.echo("Give it to the instances (certificate and key in server.yaml) or to the load balancer.")
        typer.echo(f"People connect with: latet connect {address} --fingerprint {fingerprint}")
        typer.echo("which asks for their one-time code. A connected computer gives the certificate to its agents:")
        typer.echo(f"{_AGENTS_TRUST}.")


ADMIN_TOKEN = re.compile(r"latet_adm_[A-Za-z0-9_-]{43}")
TOKEN_ID = re.compile(r"t_[a-z2-7]{10}")
ADMIN_TOKEN_ID = re.compile(r"a_[a-z2-7]{10}")
# The values an API key's own vault limit may be: the server's range (``latet.server.identities``), kept here so that a
# number outside it is refused before anything is asked of the server.
VAULT_LIMITS = (1_000, 100_000_000)
LOG_MOST = 10_000
_LOG_REQUEST_MOST = 1_000
_LOG_RECEIVE_MOST = 128 * 1024 * 1024
_LOG_RENDER_MOST = 128 * 1024 * 1024
_LOG_SECONDS = 300.0
_log_clock = time.monotonic
# server.admin.UNREADABLE_SHARED, kept here without importing the server extra on a computer.
_UNREADABLE_SHARED = (
    "the shared settings kept in the database cannot be read (written there by hand, or sealed with another key): "
    "write new ones whole, without an etag, to replace them"
)


def _stop(words: str, code: int = 2) -> NoReturn:
    typer.secho(words, fg="red", err=True)
    raise typer.Exit(code)


def _text(value: Any, most: int = 1000) -> str:
    from latet.server.client import printable

    if not isinstance(value, str) or len(value) > most:
        return "?"
    return printable(value, max(1, len(value) * 10 + 1))


def _escaped(value: str) -> str:
    from latet.server.client import printable

    return printable(value, max(1, len(value) * 10 + 1))


def _not_secret(value: str) -> None:
    from latet.engine.detectors.latet import starts_a_secret

    if starts_a_secret(value):
        _stop(
            "That is a secret, not a name, id or file. It is now in shell history: replace it with "
            "`latet server admin-token revoke`, `keys rotate`, or `people tokens --revoke`."
        )


def _no_secret_in(value: str) -> None:
    """A name or a label the server keeps in clear and lists to every administrator: refused when it holds a secret of
    Latet's anywhere, whatever its case, as the server refuses it (``holds_a_secret_start``), not only when it begins
    with one (``_not_secret``, which a file's path may hold a prefix in)."""
    from latet.engine.detectors.latet import holds_a_secret_start

    _not_secret(value)
    if holds_a_secret_start(value.casefold()):
        _stop(
            "That holds a secret, and a name is no place for one. It is now in shell history: replace it with "
            "`latet server admin-token revoke`, `keys rotate`, or `people tokens --revoke`."
        )


def _id(value: str, pattern: re.Pattern[str]) -> str:
    _not_secret(value)
    if not pattern.fullmatch(value):
        _stop("Not an id: use the id printed by the list command.")
    return value


def _mutation(method: str) -> None:
    from latet.cli.admin import _yourself

    if method.upper() != "GET":
        _yourself("latet server ...", "changes the server's administration for everyone")


def _values() -> None:
    from latet.cli.agent import agent_name

    if agent_name(strict=True) is not None:
        _stop("These settings may print values in clear: run it yourself in a terminal of your own.")
    typer.echo("These settings may print values in clear (terms, the reviewer's guidance): keep them private.",
               err=True)  # fmt: skip


def _private_problem(path: Path) -> str | None:
    from latet.storage.folders import _windows_folder_problem

    problem = _private_parent(path.parent)
    if problem is not None:
        return problem
    return _windows_folder_problem(path) if sys.platform == "win32" else None


def _private_parent(folder: Path) -> str | None:
    from latet.storage.folders import private_folder_problem

    if sys.platform == "win32":
        return private_folder_problem(folder)
    try:
        info = folder.stat()
    except OSError:
        return f"{folder} cannot be read: use a private folder of your own"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (os.getuid(), 0) or info.st_mode & 0o022:
        return f"{folder} must be a folder only you or root can write: chmod 700 {folder}"
    return None


def _private_read(path: Path) -> str:
    from latet.server.connection import NotConnected

    if problem := _private_problem(path):
        raise NotConnected(problem)
    try:
        if sys.platform == "win32":
            with path.open("rb") as file:
                raw = file.read(1025)
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise NotConnected(f"{path} must be your private regular file: chmod 600 {path}")
                raw = file.read(1025)
        return raw.decode("utf-8-sig").strip() if len(raw) <= 1024 else ""
    except (OSError, UnicodeDecodeError):
        raise NotConnected(f"{path} cannot be read as a private UTF-8 token file") from None


def _admin_token(url: str) -> str:
    from latet.cli.agent import at_your_terminal
    from latet.server.connection import NotConnected, server_url

    path = _target["token_file"]
    if path is not None:
        _not_secret(str(path))
        token = sys.stdin.readline(1025).strip() if str(path) == "-" else _private_read(Path(path))
        if token.startswith("{"):
            try:
                record = json.loads(token)
                if not isinstance(record, dict) or not isinstance(record.get("url"), str):
                    raise ValueError
                bound = server_url(record["url"])
                token = record["token"]
            except (ValueError, TypeError, KeyError):
                raise NotConnected("the administration token file holds no {url, token} record") from None
            if bound != url:
                raise NotConnected("the administration token file belongs to another server: nothing sent")
        elif not (_target["server"] or os.environ.get("LATET_SERVER_URL")):
            raise NotConnected("a token alone needs --server or LATET_SERVER_URL; nothing sent")
    elif os.environ.get("LATET_SERVER_ADMIN_TOKEN"):
        try:
            bound = server_url(os.environ.get("LATET_SERVER_URL", ""))
        except ValueError:
            bound = None
        if bound != url:
            raise NotConnected(
                "LATET_SERVER_ADMIN_TOKEN goes only to LATET_SERVER_URL's server: give this "
                "server's token with --admin-token-file, or at the prompt"
            )
        token = os.environ["LATET_SERVER_ADMIN_TOKEN"].strip()
    elif at_your_terminal():
        token = typer.prompt(f"Administration token for {url}", hide_input=True).strip()
    else:
        raise NotConnected(
            "an administration token: --admin-token-file, a hidden prompt in your terminal, or "
            "LATET_SERVER_ADMIN_TOKEN from a CI secret store"
        )
    if not isinstance(token, str) or not ADMIN_TOKEN.fullmatch(token):
        raise NotConnected("not an administration token: latet_adm_ and 43 letters, digits, - or _, alone")
    return token


ADMIN_API = "/_latet/server/v1/admin"


def admin_call(
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    query: Mapping[str, Any] | None = None,
    limit: int = 32 * 1024 * 1024,
    deadline: float = 60.0,
    receive: ReceiveBudget | None = None,
    end: float | None = None,
    timeout: float = 10.0,
) -> Any:
    """The server's administration, from any computer over HTTPS: ``path`` below ``/_latet/server/v1/admin/``
    (``"status"``), or the whole path; with the token of --admin-token-file or LATET_SERVER_ADMIN_TOKEN, at --server,
    LATET_SERVER_URL or this computer's server. Its answer is read within ``deadline`` seconds in all, each read within
    ``timeout``. Raises ServerError(status, message) for an answer that is not 2xx (status 0: nothing answered), and
    NotConnected (no server named, no token). Later parts' commands use it too."""
    from urllib.parse import unquote, urlencode

    from latet.server.client import ServerClient, answer_of, pin_certificate
    from latet.server.connection import server_target

    _mutation(method)
    where = path.removeprefix(ADMIN_API).lstrip("/")
    decoded = unquote(where)
    if (
        any(segment in (".", "..") for segment in decoded.split("/"))
        or any(c in decoded for c in "?#\\")
        or ":" in decoded
    ):
        _stop("Not an administration path: pass query parameters with query=.")
    url, pinned = server_target(_target["server"], _target["fingerprint"])
    token = _admin_token(url)
    if pinned is not None:
        pinned = pin_certificate(url, pinned)
    if query:
        where += "?" + urlencode(query)
    with ServerClient(url, None, pinned, timeout=timeout) as client:
        sent = dict(body) if body is not None else None
        accounting: dict[str, Any] = {"receive": receive} if receive is not None else {}
        if end is not None:
            deadline = min(deadline, max(0.0, end - _log_clock()))
        response = client.request(
            method,
            f"admin/{where}",
            headers={"x-latet-admin": token},
            body=sent,
            limit=limit,
            deadline=deadline,
            **accounting,
        )
    return answer_of(response)


def _ask(
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    recovery: str | None = None,
    **kwargs: Any,
) -> Any:
    """``admin_call`` for a command: a refusal said in the server's words (a token refused, where it came from), or
    silence said, and the command ended (exit 1)."""
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    _mutation(method)
    try:
        # Existing callers of _ask use a query in the short path; admin_call itself accepts only query=.
        if "?" in path:
            from urllib.parse import parse_qsl

            path, query = path.split("?", 1)
            kwargs["query"] = dict(parse_qsl(query))
        found = admin_call(method, path, body, **kwargs)
        if not isinstance(found, dict):
            raise ServerError(0, "the server's answer is not a JSON object")
        return found
    except NotConnected as exc:
        _stop(_escaped(exc.message))
    except ServerError as exc:
        if exc.status == 401:
            said = f"The server does not accept this administration token. The server refused: {_text(exc.message)}."
        elif exc.status:
            said = f"The server refused: {_text(exc.message)}."
        else:
            said = f"{_text(exc.message)}."
        typer.secho(said, fg="red", err=True)
        for detail in exc.details:
            typer.echo(_text(detail), err=True)
        if exc.status == 429 and exc.retry_after is not None:
            typer.echo(f"Try again in {exc.retry_after} seconds.", err=True)
        if exc.status == 503:
            typer.echo("The server cannot answer now: try again in a moment.", err=True)
        if (
            exc.status == 403
            and method == "POST"
            and path == "admin-tokens"
            and exc.message.startswith(OUTLIVES_ISSUER)
        ):
            typer.echo(
                "Only a token that never expires can issue another lasting token. "
                "Use one kept apart, or run `latet server init --new-admin-token --never` on the server's machine.",
                err=True,
            )
        if method != "GET" and exc.status in (0, 500):
            command = recovery or (
                "keys list" if path.endswith("/rotate") else "config get" if path == "shared" else "people list"
            )
            typer.echo(
                f"The server may have made this change. Check `latet server {command}` before trying again.", err=True
            )
        if path == "shared" and method == "GET" and exc.status == 500:
            typer.echo("`latet server config reset` replaces shared settings that cannot be opened.", err=True)
        raise typer.Exit(1) from None


def _connect_command(code: str) -> str:
    from latet.server.connection import server_target

    url, pinned = server_target(_target["server"], _target["fingerprint"])
    from latet.cli.connect import CODE

    if not isinstance(code, str) or not CODE.fullmatch(code):
        _stop("the server's answer is not a code", 1)
    return f"latet connect {url}" + (f" --fingerprint {pinned}" if pinned else "") + f"\n  {code}"


def _seen(ts: float | None) -> str:
    if ts is None:
        return "never"
    try:
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            return "?"
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "?"


def _count(value: Any) -> int | str:
    return value if type(value) is int and value >= 0 else "?"


def _object_field(answer: dict[str, Any], key: str) -> dict[str, Any]:
    value = answer.get(key)
    if not isinstance(value, dict):
        _stop(f"The server's {key} is not an object: ?", 1)
    return value


def _rows(answer: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = answer.get(key, [])
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        _stop(f"The server's {key} is not a list of objects: ?", 1)
    return value


def _identities(kind: str) -> list[dict[str, Any]]:
    from latet.server.connection import USER_ID

    answer = _ask("GET", "identities", query={"kind": kind})
    rows = _rows(answer, "identities")
    kept = []
    for row in rows:
        user = row.get("id")
        if isinstance(user, str) and USER_ID.fullmatch(user):
            kept.append(row)
        else:
            typer.echo("The server lists an identity whose id is not one: left out", err=True)
    return kept


def _target_url() -> str:
    from latet.server.connection import server_target

    return server_target(_target["server"], _target["fingerprint"])[0]


def _identity_name(identity: dict[str, Any]) -> str:
    return f"{_text(identity.get('name'))} ({_text(identity.get('id'))}, {_text(identity.get('email') or '-')})"


def _confirm(question: str, yes: bool) -> None:
    from latet.cli.agent import at_your_terminal

    if at_your_terminal():
        if not typer.confirm(question, default=False):
            _stop("Nothing changed.", 1)
    elif not yes:
        _stop("Nothing changed: run it in a terminal of your own, or add --yes.")


def _show_code(identity: dict[str, Any], code: Any) -> None:
    from latet.cli.connect import CODE
    from latet.server.connection import USER_ID

    user = identity.get("id")
    if not isinstance(user, str) or not USER_ID.fullmatch(user):
        _stop("the server's answer is not an identity", 1)
    if not isinstance(code, str) or not CODE.fullmatch(code):
        _stop("the server's answer is not a code", 1)
    typer.echo(f"{_identity_name(identity)}'s one-time code works once, within 24 hours. On their computer they run:")
    typer.echo(f"  {_connect_command(code)}")
    typer.echo("Give the code to this person alone, not in a ticket or shared channel. Whoever uses it can connect a")
    typer.echo("computer as this person and open their dashboard. Issuing it is recorded in the administration log.")


def _check_out(path: Path | None) -> None:
    if path is None:
        return
    _not_secret(str(path))
    if os.path.lexists(path):
        _stop("The output file already exists: use a new private file.")
    if problem := _private_parent(path.parent):
        _stop(_escaped(problem))


def _private_write(path: Path, text: str) -> None:
    _check_out(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
    except OSError:
        _stop("The private output file could not be written; the server may have issued the secret. Check its list.", 1)


def _show_key(
    identity: dict[str, Any], key: Any, out: Path | None, *, what: str = "API key", variables: bool = True
) -> None:
    from latet.server.connection import API_KEY, USER_ID

    user = identity.get("id")
    if (
        not isinstance(user, str)
        or not USER_ID.fullmatch(user)
        or not isinstance(key, str)
        or not API_KEY.fullmatch(key)
    ):
        _stop("the server's answer is not an API key and identity", 1)
    if out is not None:
        _private_write(out, key + "\n")
        typer.echo(f"{user}: {what} in {_escaped(str(out))}")
        return
    typer.echo(f"{_identity_name(identity)}'s {what}, shown once: keep it where the program keeps its secrets.")
    typer.echo(f"  {key}")
    if variables:
        _key_variables(key, user)


def _permissions_said(value: Any, separator: str = ",") -> str:
    """A token's permissions as the server lists them, in one word: Latet's names, sorted, ``none`` for a row allowed
    nothing, and ``?`` for an answer that is no list of Latet's names."""
    from latet.server.grants import PERMISSIONS

    if not isinstance(value, list) or any(not isinstance(name, str) or name not in PERMISSIONS for name in value):
        return "?"
    return separator.join(sorted(set(value))) or "none"


def _calls_a_model(permissions: Any, addresses: Any) -> bool:
    """Whether a token the server has just issued has an address for models: the server gave its addresses (``base``:
    to a key's first token alone, a token added to a key having none, Ruling T13b) and says it holds mask and unmask.
    Closed when the server says less or nothing at all: an answer without addresses or permissions gets no address."""
    from latet.server.grants import GATEWAY

    said = isinstance(permissions, list) and {name for name in permissions if isinstance(name, str)} >= GATEWAY
    return said and isinstance(addresses, str)


def _show_token(
    identity: dict[str, Any],
    key: Any,
    out: Path | None,
    *,
    name: Any,
    permissions: Any,
    addresses: Any,
    token_id: Any = None,
    what: str,
) -> None:
    """The secret of a token the server has just issued, shown once or written privately, with what it may do. Only a
    token that may call a model through Latet (``addresses``: the server's ``base`` for it) gets the addresses and
    variables of a key; any other is told it has none."""
    who = f"Token {_text(name)}"
    if isinstance(token_id, str) and TOKEN_ID.fullmatch(token_id):
        who += f" ({token_id})"
    typer.echo(f"{who}, permissions: {_permissions_said(permissions, ', ')}.")
    gateway = _calls_a_model(permissions, addresses)
    _show_key(identity, key, out, what=what, variables=gateway)
    if not gateway:
        typer.echo(
            "This token calls the applications API alone, so it has no address for models: an agent calls through"
            " Latet with the key's first token."
        )


@server_app.command("status")
def server_status() -> None:
    """The server's status, vault limits, log chain and edition checks."""
    from latet.config import data_dir
    from latet.engine.reviewer import certificate as certificates
    from latet.server.connection import server_target

    answer = _ask("GET", "status")
    url, pinned = server_target(_target["server"], _target["fingerprint"])
    typer.echo(f"Latet {_text(answer.get('version'))} at {url}")
    if answer.get("url") != url:
        typer.echo(f"  server names itself: {_text(answer.get('url'))}")
    if "instance" in answer:
        typer.echo(f"  instance: {_text(answer['instance'])}")
    identities = _object_field(answer, "identities")
    typer.echo(
        "  identities: " + ", ".join(f"{key} {_count(identities.get(key))}" for key in ("people", "keys", "enabled"))
    )
    if "spaces" in answer:
        typer.echo(f"  spaces: {_count(answer['spaces'])}")
    for key in ("database", "valkey"):
        if key in answer:
            shown = str(answer[key]).lower() if type(answer[key]) is bool else "?"
            typer.echo(f"  {key}: {shown}")
    vault = _object_field(answer, "vault")
    near = _rows(vault, "near")
    # Each against its own limit (an API key may have one; a server before that says none, and only the values), with
    # the fakes of its forgotten values it keeps retired, which take no room but stay, weighed with them.
    who = ", ".join(
        f"{_text(i.get('id'))}: {_count(i.get('values'))}"
        + (f" (and {_count(i['retired'])} retired fakes)" if i.get("retired") else "")
        + (f" of {_count(i['limit'])}" if "limit" in i else "")
        for i in near
    )
    plural, their = ("y", "its") if len(near) == 1 else ("ies", "their")
    typer.echo(
        f"  vault: {len(near)} identit{plural} near {their} limit"
        + (f" ({who})" if who else "")
        + f"; the server's limit is {_count(vault.get('limit'))} values"
    )
    chain = _object_field(answer, "audit").get("chain")
    typer.echo(f"  administration log: chain {_text(chain)}")
    if chain == "unknown":
        typer.echo("  no check in the last two hours")
    edition = answer.get("edition", [])
    if not isinstance(edition, list):
        _stop("The server's edition is not a list: ?", 1)
    for line in edition:
        if isinstance(line, dict):
            typer.echo(
                f"  [{_text(line.get('status'))}] {_text(line.get('title', ''))}: {_text(line.get('detail', ''))}"
            )
        elif isinstance(line, str):
            typer.echo(f"  {_text(line)}")
        else:
            typer.echo("  ?")
    if url.startswith("https://"):
        try:
            pem = certificates.load(data_dir(), pinned) if pinned else certificates.fetch(url)[0]
            if pem:
                seen = "pinned certificate" if pinned else "certificate this computer sees"
                typer.echo(f"  {seen}: expires {certificates.expires(pem).date().isoformat()}")
        except (OSError, ValueError):
            typer.echo("  certificate: could not be read")


people_cli = typer.Typer(help="The people who use the server, and their computers.", no_args_is_help=True)
keys_cli = typer.Typer(help="API keys: identities of programs that belong to no person.", no_args_is_help=True)
server_app.add_typer(people_cli, name="people")
server_app.add_typer(keys_cli, name="keys")
Person = Annotated[str, typer.Argument(help="The person: their id, name or e-mail address.")]
Key = Annotated[str, typer.Argument(help="The API key: its id or name.")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Confirm in a script; your own terminal still asks.")]


def _find(kind: str, who: str) -> dict[str, Any]:
    from latet.server.connection import USER_ID

    _not_secret(who)
    if who.startswith("u_") and not USER_ID.fullmatch(who):
        _stop("Not an identity id: use the id from the list command.")
    everyone = _identities(kind)
    folded = who.casefold()
    if USER_ID.fullmatch(who):
        found = [i for i in everyone if i["id"] == who]
    else:
        found = [
            i
            for i in everyone
            if any(isinstance(i.get(key), str) and i[key].casefold() == folded for key in ("name", "email"))
        ]
    group = "people" if kind == "person" else "keys"
    if not found:
        _stop(f"Nobody here is {_text(who)}: `latet server {group} list`.", 1)
    if len(found) > 1:
        ids = ", ".join(i["id"] for i in found)
        _stop(f"{_text(who)} names {len(found)} identities: use the id ({ids}).", 1)
    return found[0]


def _set_disabled(kind: str, who: str, disabled: bool) -> None:
    found = _find(kind, who)
    _ask(
        "PATCH",
        f"identities/{found['id']}",
        {"disabled": disabled},
        recovery="keys list" if kind == "key" else "people list",
    )
    state = "disabled" if disabled else "enabled"
    typer.echo(
        f"{_text(found.get('name'))} ({found['id']}) is {state}: the change reaches "
        f"every instance within seconds. Its space is kept."
    )


def _remove(kind: str, who: str, yes: bool) -> None:
    found = _find(kind, who)
    _confirm(
        f"Remove {_identity_name(found)} from {_target_url()}, and delete their space: vault, history, settings?", yes
    )
    _ask("DELETE", f"identities/{found['id']}", recovery="keys list" if kind == "key" else "people list")
    typer.echo(f"{_identity_name(found)} is removed, and their space deleted.")


def _key_variables(key: str, name: str) -> None:
    from latet import wrappers
    from latet.server.tokens import route_key

    typer.echo("The program's variables (they carry the key's route: keep them as secret as the key):")
    for variable, value in wrappers.generic_env(base=f"{_target_url()}/u/{route_key(key)}").items():
        typer.echo(f"  {variable}={value}")
    typer.echo("Kept in a file, `latet env --key-file <file>` prints them again.")
    typer.echo(f"Its settings: latet server config --key {name} (and latet server rules --key {name}).")


@people_cli.command("add")
def people_add(
    name: Annotated[str, typer.Argument(help="The person's name.")],
    email: Annotated[str | None, typer.Option(help="Their e-mail address.")] = None,
) -> None:
    """Add a person and show their one-time code, to be given to them alone."""
    _not_secret(name)
    answer = _ask("POST", "identities", {"kind": "person", "name": name, "email": email})
    _show_code(_object_field(answer, "identity"), answer.get("code"))


@people_cli.command("list")
def people_list() -> None:
    """Everyone: enabled or not, computers connected, last seen and tampered token rows."""
    people = _identities("person")
    if not people:
        typer.echo('Nobody yet: latet server people add "Emily Carter" --email emily.carter@acme.com')
    invalid = False
    for p in people:
        state = "disabled" if p.get("disabled") is True else "enabled"
        seen = _seen(p.get("last_seen"))
        invalid |= seen == "?"
        typer.echo(
            f"{p['id']}  {_text(p.get('name'))}  {_text(p.get('email') or '-')}  "
            f"{state}  {_count(p.get('tokens'))} computers, seen {seen}"
        )
        if p.get("tampered_tokens"):
            typer.echo(f"  {_count(p['tampered_tokens'])} tampered token rows: they open nothing")
    if invalid:
        raise typer.Exit(1)


@people_cli.command("disable")
def people_disable(person: Person) -> None:
    """Disable a person: every token of theirs stops on every instance within seconds; their space is kept."""
    _set_disabled("person", person, True)


@people_cli.command("enable")
def people_enable(person: Person) -> None:
    """Enable a person again: their tokens and their space work again."""
    _set_disabled("person", person, False)


@people_cli.command("remove")
def people_remove(person: Person, yes: Yes = False) -> None:
    """Remove a person: their tokens stop, and their space (vault, history, settings) is deleted, once you agree."""
    _remove("person", person, yes)


@people_cli.command("code")
def people_code(person: Person) -> None:
    """A new one-time code: whoever uses it can connect as this person and open their dashboard; recorded."""
    found = _find("person", person)
    answer = _ask("POST", f"identities/{found['id']}/code")
    _show_code(found, answer.get("code"))


@people_cli.command("tokens")
def people_tokens(
    person: Person,
    revoke: Annotated[str | None, typer.Option(help="Revoke this token's id.")] = None,
    revoke_all: Annotated[bool, typer.Option("--revoke-all", help="Revoke every token of theirs.")] = False,
    yes: Yes = False,
) -> None:
    """A person's computers; --revoke or --revoke-all stops their tokens."""
    if revoke:
        _id(revoke, TOKEN_ID)
    if revoke and revoke_all:
        _stop("Use --revoke or --revoke-all.")
    found = _find("person", person)
    if revoke or revoke_all:
        if revoke_all:
            _confirm(
                f"Revoke all tokens of {_identity_name(found)} on {_target_url()}? Every computer needs a new code.",
                yes,
            )
        query = {"token": revoke} if revoke else None
        answer = _ask(
            "DELETE",
            f"identities/{found['id']}/tokens",
            recovery=f"people tokens {found['id']}",
            **({"query": query} if query else {}),
        )
        count = _count(answer.get("revoked"))
        if count == 0:
            _stop(
                f"No token of {_identity_name(found)} has that id: nothing was revoked "
                f"(`latet server people tokens {found['id']}` lists theirs)",
                1,
            )
        if count == "?":
            _stop("The server's revoked count is not a count: ?", 1)
        typer.echo(f"{count} tokens of {_identity_name(found)} revoked: those computers need a new code.")
        return
    for token in _rows(_ask("GET", f"identities/{found['id']}/tokens"), "tokens"):
        token_id = token.get("id")
        shown = token_id if isinstance(token_id, str) and TOKEN_ID.fullmatch(token_id) else "?"
        typer.echo(f"{shown}  {_text(token.get('label'))} last used {_seen(token.get('last_used'))}")
        if token.get("tampered") is True:
            typer.echo("  its row does not hold: made or changed without the server's key, it opens nothing")


@keys_cli.command("add")
def keys_add(
    name: Annotated[str, typer.Argument(help="What the key is for, such as Billing CI.")],
    out: Annotated[Path | None, typer.Option(help="Write the key privately; print no secret.")] = None,
) -> None:
    """Make an API key, shown once or written privately with --out."""
    _not_secret(name)
    _check_out(out)
    answer = _ask("POST", "identities", {"kind": "key", "name": name}, recovery="keys list")
    identity = _object_field(answer, "identity")
    _show_key(identity, answer.get("key"), out)


@keys_cli.command("list")
def keys_list() -> None:
    """Every API key: enabled or not, when it was last used, and its vault limit if it has one of its own."""
    keys = _identities("key")
    if not keys:
        typer.echo('No API key yet: latet server keys add "Billing CI"')
    for key in keys:
        state = "disabled" if key.get("disabled") is True else "enabled"
        own = "" if key.get("vault_limit") is None else f"  vault limit {_count(key['vault_limit'])}"
        typer.echo(f"{key['id']}  {_text(key.get('name'))}  {state}  last used {_seen(key.get('last_seen'))}{own}")


def _key_tokens(found: dict[str, Any]) -> list[dict[str, Any]]:
    """The tokens the server lists for an API key, as it answers them."""
    return _rows(_ask("GET", f"identities/{found['id']}/tokens"), "tokens")


def _token_of(found: dict[str, Any], tokens: list[dict[str, Any]], token_id: str, done: str) -> dict[str, Any]:
    """The key's token with this id, among the ones listed; none is a stop, and nothing is sent."""
    for token in tokens:
        if token.get("id") == token_id:
            return token
    _stop(
        f"No token of {_identity_name(found)} has that id: nothing was {done} "
        f"(`latet server keys tokens {found['id']}` lists theirs)",
        1,
    )


@keys_cli.command("tokens")
def keys_tokens(
    key: Key,
    revoke: Annotated[str | None, typer.Option(help="Revoke this token's id.")] = None,
    yes: Yes = False,
) -> None:
    """An API key's tokens: each one's name, what it may do, whether it serves the key's agents (has an address), and
    when it was last used; --revoke stops one."""
    if revoke is not None:
        _id(revoke, TOKEN_ID)
    found = _find("key", key)
    tokens = _key_tokens(found)
    if revoke is not None:
        named = _token_of(found, tokens, revoke, "revoked")
        _confirm(
            f"Revoke the token {revoke} ({_text(named.get('name', named.get('label')))}) of {_identity_name(found)} "
            f"on {_target_url()}? Whatever uses its secret or its address stops within seconds.",
            yes,
        )
        answer = _ask(
            "DELETE",
            f"identities/{found['id']}/tokens",
            recovery=f"keys tokens {found['id']}",
            query={"token": revoke},
        )
        count = _count(answer.get("revoked"))
        if count == 0:
            _stop(
                f"No token of {_identity_name(found)} has that id any more: nothing was revoked "
                f"(`latet server keys tokens {found['id']}` lists theirs)",
                1,
            )
        if count == "?":
            _stop("The server's revoked count is not a count: ?", 1)
        typer.echo(f"The token {revoke} of {_identity_name(found)} is revoked: its secret and its address stop.")
        return
    if not tokens:
        typer.echo(f"{_identity_name(found)} has no token: `latet server keys rotate {found['id']}` makes it one.")
    for token in tokens:
        token_id = token.get("id")
        shown = token_id if isinstance(token_id, str) and TOKEN_ID.fullmatch(token_id) else "?"
        name, permissions = _text(token.get("name", token.get("label"))), _permissions_said(token.get("permissions"))
        # The key's first token (and every token made before tokens had permissions) serves its agents through its
        # address: said, so that it is not revoked by mistake for one added with the same permissions (final review M2).
        agents = ", serves agents (has an address)" if token.get("agents") is True else ""
        typer.echo(f"{shown}  {name} {permissions}{agents} last used {_seen(token.get('last_used'))}")
        if token.get("tampered") is True:
            typer.echo("  its row does not hold: made or changed without the server's key, it opens nothing")


@keys_cli.command("add-token")
def keys_add_token(
    key: Key,
    name: Annotated[str, typer.Argument(help="What the token is for, such as logs.")],
    scan: Annotated[bool, typer.Option("--scan", help="It may say what a text holds, and change nothing.")] = False,
    mask: Annotated[
        bool, typer.Option("--mask", help="It may replace what a text holds with fakes (and so scan it).")
    ] = False,
    unmask: Annotated[
        bool, typer.Option("--unmask", help="It may put the real values back in place of the fakes.")
    ] = False,
    forget: Annotated[
        bool,
        typer.Option("--forget", help="It may take values out of the key's vault; no other permission brings it."),
    ] = False,
    out: Annotated[Path | None, typer.Option(help="Write the token privately; print no secret.")] = None,
) -> None:
    """Give an API key another token with the permissions you choose, shown once or written privately with --out.

    A token added so calls the applications API alone, where each unmask is in the administration log: it has no
    address for models, whatever it may do, and agents use the key's first token. --forget is given by name alone: the
    key's first token cannot forget. Each token stops on its own: `keys tokens` lists them, `keys tokens --revoke`
    stops one.
    """
    _no_secret_in(name)
    asked = (("scan", scan), ("mask", mask), ("unmask", unmask), ("forget", forget))
    chosen = [word for word, wanted in asked if wanted]
    if not chosen:
        _stop("give at least one of --scan, --mask, --unmask and --forget")
    _check_out(out)
    found = _find("key", key)
    answer = _ask(
        "POST",
        f"identities/{found['id']}/tokens",
        {"name": name, "permissions": chosen},
        recovery=f"keys tokens {found['id']}",
    )
    token = _object_field(answer, "token")
    kept = token.get("name", name)
    _show_token(
        found,
        answer.get("key"),
        out,
        name=kept,
        permissions=token.get("permissions"),
        addresses=answer.get("base"),
        token_id=token.get("id"),
        what=f"token {_text(kept)}",
    )


@keys_cli.command("rotate")
def keys_rotate(
    key: Key,
    token: Annotated[
        str | None,
        typer.Option(help="The token to rotate: its id, as `keys tokens` lists it. A key with several needs it."),
    ] = None,
    yes: Yes = False,
    out: Annotated[Path | None, typer.Option(help="Write the new key privately; print no secret.")] = None,
) -> None:
    """Rotate one of an API key's tokens: its old secret and addresses stop on every instance within seconds.

    The token keeps its name and its permissions. A key with several tokens needs --token; a key with one rotates
    that one, which this command names to the server after listing it.
    """
    if token is not None:
        _id(token, TOKEN_ID)
    _check_out(out)
    found = _find("key", key)
    tokens = _key_tokens(found)
    named: dict[str, Any] | None = None
    if token is not None:
        named = _token_of(found, tokens, token, "rotated")
    elif len(tokens) == 1:
        named = tokens[0]
        if not isinstance(named.get("id"), str) or not TOKEN_ID.fullmatch(named["id"]):
            _stop("The server lists a token whose id is not one: nothing was rotated.", 1)
    which = f"the token {_text(named.get('name', named.get('label')))} ({named['id']}) of " if named else ""
    _confirm(
        f"Rotate {which}{_identity_name(found)} on {_target_url()}? The old key and its addresses stop within seconds.",
        yes,
    )
    answer = _ask(
        "POST",
        f"identities/{found['id']}/rotate",
        {"token": named["id"]} if named else None,
        recovery=f"keys tokens {found['id']}",
    )
    typer.echo("The old key and its addresses stop on every instance within seconds.")
    _show_token(
        found,
        answer.get("key"),
        out,
        name=answer.get("name", "key"),
        permissions=answer.get("permissions"),
        addresses=answer.get("base"),
        token_id=answer.get("token"),
        what="API key",
    )


def _vault_limit(given: str) -> int | None:
    """The limit a command line gives: a whole number of values within the server's range, or None for ``default``.
    Anything else is refused, in words that give the range, before anything is asked of the server."""
    if given == "default":
        return None
    least, most = VAULT_LIMITS
    if re.fullmatch(r"[0-9]{1,12}", given) and least <= int(given) <= most:  # ASCII digits alone
        return int(given)
    _stop(f"A vault limit is a whole number of values from {least} to {most}, or default for the server's limit.")


@keys_cli.command("limit")
def keys_limit(
    key: Key,
    limit: Annotated[
        str,
        typer.Argument(
            help="How many values its vault may hold (1000 to 100000000), or default for the server's limit.",
            metavar="N|default",
        ),
    ],
) -> None:
    """Set how many values an API key's vault may hold, or give it the server's limit again.

    A key with no limit of its own has the server's (vault_limit). A vault that holds the most it may refuses the
    call that would add a value (the applications API answers 507) and lets nothing of that call out. A limit below
    the values the vault holds is kept, and the vault takes no new value until the limit is raised or values are
    forgotten (`keys forget`). The change reaches every instance within seconds.
    """
    wanted = _vault_limit(limit)
    found = _find("key", key)
    answer = _ask("PATCH", f"identities/{found['id']}", {"vault_limit": wanted}, recovery="keys list")
    identity = _object_field(answer, "identity")
    if identity.get("vault_limit") != wanted:  # an answer that does not show the change is no success
        shown = "the server's" if wanted is None else wanted
        _stop(f"The server's answer does not show the vault limit ({shown}): check `latet server keys list`.", 1)
    who = f"{_text(found.get('name'))} ({found['id']})"
    if wanted is None:
        typer.echo(f"{who} has the server's vault limit again: the change reaches every instance within seconds.")
        return
    typer.echo(f"{who} may hold {wanted} values in its vault: the change reaches every instance within seconds.")
    held = identity.get("vault_values")
    if type(held) is int and held > wanted:
        typer.echo(
            f"Its vault holds {held} values already: it takes no new value until the limit is raised or values "
            "are forgotten (`latet server keys forget`)."
        )


FORGET_AT_ONCE = 1000  # values one forget of the administration takes at most (the applications API's MOST_TEXTS)
# Seconds a call of ``keys forget`` waits for the server's answer, each read and in all: a key's first forget after an
# upgrade gives the local reviewer's older answers and the history's older rows their keyed hashes first (review 11b's
# N5), minutes on a key with much history, where every other call waits 10 s for a read. One that stops waiting is safe
# to run again: what the server forgot stays forgotten, counted as not held then.
FORGET_SECONDS = 300.0


def _values_to_forget(file: Path | None) -> list[str]:
    """The values a forget names, each once, in the order given: one a line of ``file`` (UTF-8, a byte-order mark and
    the line endings left out, an empty line skipped), or typed in your terminal, an empty line ending them; never from
    the command line, which a shell keeps in its history. Stops, nothing sent, when there is neither, when the file
    cannot be read, or when no value is given; its words quote none of them."""
    from latet.cli.agent import at_your_terminal

    if file is not None:
        try:
            text = file.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            _stop(f"{_escaped(str(file))} cannot be read as a UTF-8 text file: nothing was sent.")
        lines = [line.removesuffix("\r") for line in text.split("\n")]
    elif at_your_terminal():
        typer.echo("Type the values to forget, one a line; an empty line ends them.", err=True)
        lines = []
        while (line := sys.stdin.readline().removesuffix("\n").removesuffix("\r")) != "":
            lines.append(line)
    else:
        _stop(
            "Give the values to forget in a file, one a line (--file FILE), or type them in a terminal of your own: "
            "never on the command line, which a shell keeps in its history."
        )
    values = list(dict.fromkeys(line for line in lines if line))
    if not values:
        _stop("No value to forget was given: nothing was sent.")
    return values


def _forget_count(answer: dict[str, Any], key: str) -> int:
    """A count of the server's answer to a forget: a whole number, or a stop."""
    found = answer.get(key)
    if type(found) is not int or found < 0:
        _stop(f"The server's {key} count is not a count: ?", 1)
    return found


@keys_cli.command("forget")
def keys_forget(
    key: Key,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file", help="The values to forget, one a line (UTF-8); without it, type them in your terminal."
        ),
    ] = None,
    yes: Yes = False,
) -> None:
    """Forget values of an API key's vault: each taken out of it on every instance within seconds, once you agree.

    The values are read one a line from --file, or typed in your terminal, never from the command line. Each is found
    exactly as written: another case or another written form is another value of the vault. A value forgotten is gone
    with its fake, which restores nothing from then on, and it gets a new fake if it is met again. An address at a
    company's domain keeps the domain as a value of its own: forget it too if it should go. Each call is recorded in
    the administration log (key.forgotten) with its counts, never a value. A key's first forget after an upgrade can
    take minutes on a key with much history: each call waits up to five minutes, and running it again is safe.
    """
    from latet.cli.agent import agent_name

    if agent_name(strict=True) is not None:
        _stop("The values to forget are personal data: run it yourself in a terminal of your own.")
    values = _values_to_forget(file)
    found = _find("key", key)
    many = "" if len(values) == 1 else "s"
    _confirm(
        f"Forget {len(values)} value{many} from the vault of {_identity_name(found)} on {_target_url()}? Their fakes "
        "will restore nothing.",
        yes,
    )
    forgotten = unknown = 0
    for start in range(0, len(values), FORGET_AT_ONCE):
        try:
            answer = _ask(
                "POST",
                f"identities/{found['id']}/forget",
                {"values": values[start : start + FORGET_AT_ONCE]},
                recovery="log --action key.forgotten",
                timeout=FORGET_SECONDS,
                deadline=FORGET_SECONDS,
            )
        except typer.Exit:
            if start:
                were = "value was" if forgotten == 1 else "values were"
                typer.echo(f"{forgotten} {were} forgotten before this, by the calls that went through.", err=True)
            raise
        forgotten += _forget_count(answer, "forgotten")
        unknown += _forget_count(answer, "unknown")
    who = f"{_text(found.get('name'))} ({found['id']})"
    gone = f"{forgotten} value{'' if forgotten == 1 else 's'} forgotten"
    typer.echo(f"{gone} from the vault of {who}; {unknown} {'was' if unknown == 1 else 'were'} not in it.")


@keys_cli.command("disable")
def keys_disable(key: Key) -> None:
    """Disable an API key: its addresses stop on every instance within seconds; its space is kept."""
    _set_disabled("key", key, True)


@keys_cli.command("enable")
def keys_enable(key: Key) -> None:
    """Enable an API key again."""
    _set_disabled("key", key, False)


@keys_cli.command("remove")
def keys_remove(key: Key, yes: Yes = False) -> None:
    """Remove an API key: it stops, and its space is deleted, once you agree."""
    _remove("key", key, yes)


config_cli = typer.Typer(
    help="The settings the server shares: destinations, the reviewer, text recognition, history, retention (--key: an "
    "API key's own settings and rules, the keys of `latet config`).",
    no_args_is_help=True,
)
rules_cli = typer.Typer(
    help="An API key's own recognizers and rules (--key). Rules for everyone are planned for the Enterprise Edition.",
    no_args_is_help=True,
)
token_cli = typer.Typer(help="The server's administration tokens.", no_args_is_help=True)
server_app.add_typer(config_cli, name="config")
server_app.add_typer(rules_cli, name="rules")
server_app.add_typer(token_cli, name="admin-token")
# Whose settings `latet server config` and `latet server rules` change: everyone's, or one API key's (--key).
_whose: dict[str, Any] = {"key": None}


def _whose_option(
    key: Annotated[
        str | None,
        typer.Option("--key", help="An API key's own settings (its id or name), on top of the shared ones."),
    ] = None,
) -> None:
    if key is not None:
        _not_secret(key)
    _whose.clear()
    _whose["key"] = key


config_cli.callback()(_whose_option)
rules_cli.callback()(_whose_option)


def _shared_only(key: str | None) -> None:
    """Without --key, a key of the settings the server shares, or none (every one): any other is refused before
    anything is asked of the server, a rule for everyone with the ways to share rules by hand."""
    from latet.server.shared import OWN_KEYS, RULE_KEYS, RULES_FOR_EVERYONE, SERVER_KEYS, SETTING_KEYS

    if _whose.get("key") is not None or key is None or (top := key.split(".")[0]) in SETTING_KEYS:
        return
    shared = ", ".join(name for name in SETTING_KEYS if name not in SERVER_KEYS)
    if top in RULE_KEYS:
        _stop(f"{_text(key)}: {RULES_FOR_EVERYONE}.")
    if top in OWN_KEYS:
        _stop(f"{_text(key)}: each identity's own, never shared (the server shares {shared}).")
    _stop(f"{_text(key)}: not a setting the server shares ({shared}).")


def _own_rules_only() -> None:
    """An API key's own rules need --key: rules for everyone are the Enterprise Edition's."""
    from latet.server.shared import RULES_FOR_EVERYONE

    if _whose.get("key") is None:
        _stop(f"Use --key KEY for an API key's own rules: {RULES_FOR_EVERYONE}.")


def _place() -> tuple[str, str]:
    """Where the settings these commands change are, and their name: the shared ones, or with --key an API key's
    (the shared ones with its own on top, sent whole: the server keeps its own part)."""
    if _whose.get("key") is None:
        return "shared", "The shared settings"
    if "found" not in _whose:
        _whose["found"] = _find("key", _whose["key"])
    found = _whose["found"]
    return f"identities/{found['id']}/settings", f"{_text(found.get('name'))}'s settings ({found['id']})"


def _kept_read(kept: Any) -> dict[str, Any] | None:
    """The rules for everyone the server says an earlier version kept (none when it says nothing), if they read as
    such: rules by their names, a list for each list."""
    from latet.server.shared import RULE_KEYS, UNION_KEYS

    if kept is None:
        return {}
    if not isinstance(kept, dict) or not all(isinstance(key, str) and key in RULE_KEYS for key in kept):
        return None
    if any(not isinstance(value, list) for key, value in kept.items() if key in UNION_KEYS):
        return None
    return kept


def _shared() -> tuple[dict[str, Any], str]:
    answer = _ask("GET", _place()[0])
    data, etag = answer.get("settings"), answer.get("etag")
    if not isinstance(data, dict) or not isinstance(etag, str):
        _stop("The server's settings answer is not settings with an etag.", 1)
    if (kept := _kept_read(answer.get("kept_rules"))) is None:
        _stop("The server's answer names rules kept from an earlier version that are not rules.", 1)
    _whose["kept"] = kept  # the rules for everyone an earlier version kept
    return data, etag


def _shared_config(data: dict[str, Any]) -> Config:
    from pydantic import ValidationError

    from latet.config import Config

    try:
        return Config.model_validate(data)
    except ValidationError:
        where = "config get" if _whose.get("key") is None else f"config --key {_whose['found']['id']} get"
        _stop(f"These settings are not valid settings: inspect them with `latet server {where}`.", 1)


def _write_shared(data: dict[str, Any], etag: str) -> None:
    """The shared settings, checked here first, then sent whole with the etag they were read with (the server would
    keep a key left out as it is: every key is sent, as read)."""
    from pydantic import ValidationError

    from latet.config import Config
    from latet.rules_report import ERRORS

    try:
        parsed = Config.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            typer.secho(_escaped(f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"), fg="red")
        raise typer.Exit(ERRORS) from None
    from latet.config import term_errors

    if errors := term_errors(t.value for t in parsed.terms):
        for error in errors:
            typer.echo(_text(error["msg"]), err=True)
        raise typer.Exit(ERRORS)
    path, name = _place()
    recovery = "config get" if path == "shared" else f"config --key {_whose['found']['id']} get"
    _ask("PUT", path, {**data, "etag": etag}, recovery=recovery)  # settings and the etag they were read with
    whose = "every space applies" if path == "shared" else "its space applies"
    typer.echo(f"{name} changed: {whose} them at once.")


@config_cli.command("get")
def shared_get(
    key: Annotated[str | None, typer.Argument(help="A key, with dots for what is inside (none: every key).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print it as JSON.")] = False,
) -> None:
    """Print a shared setting (or every one)."""
    import yaml

    from latet.cli.rules import _at
    from latet.server.shared import kept_said

    _shared_only(key)
    _values()
    data, _ = _shared()
    if kept := _whose.get("kept"):
        typer.secho(
            _escaped(
                f"The shared settings keep rules for everyone from an earlier version ({kept_said(kept)}), applied by "
                "no space: `latet server rules export` writes them, `latet server rules forget` deletes them."
            ),
            fg="yellow",
            err=True,
        )
    value: Any = data
    if key:
        node, last = _at(data, key)
        value = node[last]
    if as_json:
        typer.echo(json.dumps(value, indent=2, ensure_ascii=True))
    else:
        dumped = yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip().removesuffix("...").rstrip()
        typer.echo("\n".join(_escaped(line) for line in dumped.split("\n")))


@config_cli.command("set")
def shared_set(
    key: Annotated[
        str,
        typer.Argument(help="A key, with dots for what is inside: `history`, `ocr` (--key: `profile`)."),
    ],
    value: Annotated[str | None, typer.Argument(help="The new value, in YAML.")] = None,
    unset: Annotated[bool, typer.Option("--unset", help="Back to its default.")] = False,
) -> None:
    """Change a shared setting: every space applies it at once."""
    import yaml

    from latet.cli.rules import _at
    from latet.config import load_value

    if (value is None) == (not unset):
        raise typer.BadParameter("give a VALUE, or --unset")
    _shared_only(key)
    data, etag = _shared()
    node, last = _at(data, key)
    if unset and node is data:  # the server keeps a key a write leaves out: a top-level one is sent as its default
        from latet.config import Config

        node[last] = Config().model_dump(mode="json")[str(last)]
    elif unset:  # a key inside another: its parent is sent whole, without it
        del node[last]
    else:
        try:
            node[last] = load_value(value or "")
        except yaml.YAMLError as exc:
            raise typer.BadParameter(f"not valid YAML: {exc}", param_hint="VALUE") from None
    _write_shared(data, etag)
    if key == "llm_review.chunk_chars":
        typer.echo("The server reads chunk_chars above 16384 as 16384.")


@config_cli.command("reset")
def shared_reset(yes: Yes = False) -> None:
    """Replace an unreadable shared settings row with defaults; never reset settings that open."""
    from latet.config import Config
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    _mutation("PUT")
    if _whose.get("key") is not None:
        _stop("Reset applies only to shared settings.")
    try:
        admin_call("GET", "shared")
    except ServerError as exc:
        if exc.status != 500 or exc.message != _UNREADABLE_SHARED:
            _stop(f"The server refused: {_text(exc.message)}", 1)
    except NotConnected as exc:
        _stop(_escaped(exc.message))
    else:
        _stop("These settings open: use `latet server config set --unset`, or `latet server rules export` first.", 1)
    _confirm(f"Replace what the database holds unread on {_target_url()} with default shared settings?", yes)
    from latet.server.shared import SETTING_KEYS

    dumped = Config().model_dump(mode="json")
    _ask("PUT", "shared", {key: dumped[key] for key in SETTING_KEYS})
    typer.echo("The unread shared settings were replaced with defaults.")


@config_cli.command("add")
def shared_add(
    key: Annotated[str, typer.Argument(help="A list: `upstreams` (--key: `terms`, `allow_values`...).")],
    value: Annotated[str, typer.Argument(help="The item, in YAML: `{name: llm, url: 'https://llm.acme.com'}`.")],
) -> None:
    """Add an item to a list of the shared settings (--key: of an API key's): every space has it at once."""
    from latet.cli.rules import _at
    from latet.config import load_value

    _shared_only(key)
    data, etag = _shared()
    node, last = _at(data, key)
    if not isinstance(node[last], list):
        raise typer.BadParameter(f"{key} is not a list: `latet server config set` changes it", param_hint="KEY")
    item = load_value(value)
    if _listed_already(data, key, item):
        typer.echo(f"{key}: that item is there already, so nothing changed.")
        return
    node[last].append(item)
    _write_shared(data, etag)


def _listed_already(data: dict[str, Any], key: str, item: Any) -> bool:
    """Whether the list at ``key`` holds ``item`` already, both read as settings are: the server lists a term with its
    defaults, and the same term given without them is the same item. Settings that do not read so are left to the
    write's own check, ``item`` compared as it was given."""
    import copy

    from pydantic import ValidationError

    from latet.cli.rules import _at
    from latet.config import Config

    node, last = _at(data, key)
    if item in node[last]:
        return True
    candidate = copy.deepcopy(data)
    place, name = _at(candidate, key)
    place[name] = [*place[name], item]
    try:
        read = Config.model_validate(candidate).model_dump(mode="json")
        place, name = _at(read, key)
    except (ValidationError, KeyError, TypeError, typer.BadParameter):
        return False
    items = place[name]
    return isinstance(items, list) and len(items) > 1 and items[-1] in items[:-1]


@config_cli.command("remove")
def shared_remove(
    key: Annotated[str, typer.Argument(help="A list: `upstreams` (--key: `terms`, `allow_values`...).")],
    value: Annotated[str, typer.Argument(help="The item to take out; for terms and recognizers, its value or name.")],
) -> None:
    """Take an item out of a list of the shared settings (--key: of an API key's)."""
    from latet.cli.rules import _at
    from latet.config import load_value

    _shared_only(key)
    data, etag = _shared()
    node, last = _at(data, key)
    items = node[last]
    if not isinstance(items, list):
        raise typer.BadParameter(f"{key} is not a list: `latet server config set --unset` resets it", param_hint="KEY")
    wanted = load_value(value)
    kept = [
        i
        for i in items
        if not (i in (wanted, value) or (isinstance(i, dict) and value in (i.get("value"), i.get("name"))))
    ]
    if len(kept) == len(items):
        typer.echo(f"{key}: not there")
        raise typer.Exit(1)
    node[last] = kept
    _write_shared(data, etag)


@rules_cli.command("add")
def shared_rules_add(file: Annotated[Path, typer.Argument(help="A YAML file with one or more recognizers.")]) -> None:
    """Add an API key's recognizers (or update those with the same name), checked here first as `latet rules test`
    does. Their examples are not checked against a vault: invent them."""
    from latet.cli.rules import _entries, _print
    from latet.rules_report import ERRORS, outcome, report

    _own_rules_only()
    entries = _entries(file)
    checked = report(entries)
    if outcome(checked) == ERRORS:
        _print(checked)
        typer.secho("Nothing was added: fix the errors above (`latet rules test FILE`).", fg="red")
        raise typer.Exit(ERRORS)
    data, etag = _shared()
    current = list(_rows(data, "recognizers"))
    if any(not isinstance(entry.get("name"), str) for entry in current):
        _stop("The server lists a recognizer whose name is not text: repair its settings before adding rules.", 1)
    by_name = {entry.get("name"): i for i, entry in enumerate(current) if isinstance(entry, dict)}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name in by_name:
            current[by_name[name]] = entry
        else:
            current.append(entry)
            by_name[name] = len(current) - 1
        typer.echo(f"{_text(name)}: checked")
    data["recognizers"] = current
    _write_shared(data, etag)


@rules_cli.command("list")
def shared_rules_list(as_json: Annotated[bool, typer.Option("--json", help="Print them as JSON.")] = False) -> None:
    """An API key's recognizers, and whether each one loads."""
    from latet.engine.build import reserved_types
    from latet.engine.detectors.recognizers import compile_all

    _own_rules_only()
    entries = _rows(_shared()[0], "recognizers")
    _, statuses = compile_all(entries, reserved_types())
    if as_json:
        typer.echo(json.dumps([{"name": s.name, "state": s.state} for s in statuses], indent=2, ensure_ascii=True))
        return
    if not statuses:
        typer.echo(f"No recognizers here. `latet server rules --key {_whose['found']['id']} add FILE` adds some.")
    for status in statuses:
        typer.echo(f"{_text(status.name):24s} {_text(status.state)}")


@rules_cli.command("remove")
def shared_rules_remove(name: Annotated[str, typer.Argument(help="The recognizer's name.")]) -> None:
    """Remove an API key's recognizer."""
    _own_rules_only()
    data, etag = _shared()
    current = list(_rows(data, "recognizers"))
    kept = [e for e in current if not (isinstance(e, dict) and e.get("name") == name)]
    if len(kept) == len(current):
        typer.secho(f"No recognizer of this key is called {_text(name)}.", fg="red")
        raise typer.Exit(1)
    data["recognizers"] = kept
    _write_shared(data, etag)


@rules_cli.command("import")
def shared_rules_import(
    file: Annotated[Path, typer.Argument(help="A rules file (`latet rules export`), or a config.yaml or latet.yaml.")],
    everything: Annotated[bool, typer.Option("--all", help="Also what lowers protection.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only say what it would do.")] = False,
) -> None:
    """Add a rules file to an API key's settings: what adds protection; with --all, what lowers it too."""
    from latet.cli.rules import _print_import
    from latet.rules_report import ERRORS
    from latet.transfer import RulesError, merge_rules, read_rules

    _own_rules_only()
    try:
        settings, notices = read_rules(file.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, RulesError) as exc:
        typer.secho(f"Nothing was imported: {exc}.", fg="red")
        raise typer.Exit(ERRORS) from None
    data, etag = _shared()
    merged = merge_rules(_shared_config(data), settings, None)
    merged.report.notices = notices
    shown = {key: [_escaped(line) for line in lines] for key, lines in merged.report.as_dict().items()}
    _print_import(shown, everything)
    if dry_run:
        return
    chosen = merged.full if everything else merged.protective
    if not merged.report.added and not (everything and merged.report.held):
        typer.echo("Nothing to import.")
        return
    _write_shared(chosen.model_dump(mode="json"), etag)


@rules_cli.command("export")
def shared_rules_export(
    file: Annotated[Path | None, typer.Argument(help="Where to write them (none: here).")] = None,
) -> None:
    """Write an API key's rules to a file (--key; the terms are values in clear), to share them by hand (`latet rules
    import`, or Import in the dashboard)."""
    from latet.transfer import export_rules

    _values()  # a file too: an agent would read it back, as `config get` is refused to it
    if file is not None:
        _check_out(file)
    data, _ = _shared()
    if _whose.get("key") is None:  # the rules for everyone an earlier version kept, if the server keeps some
        if not (kept := _whose.get("kept")):
            _own_rules_only()  # without --key: stops
            return
        data = kept
        if left := _not_in_a_file(kept):
            said = f"A rules file does not carry {', '.join(left)}: not in the file."
            typer.secho(_escaped(said), fg="yellow", err=True)
    text, warnings = export_rules(_shared_config(data))
    if file is None:
        typer.echo("\n".join(_escaped(line) for line in text.split("\n")), nl=False)
    else:
        _private_write(file, text)
        typer.echo(f"The rules are in {_escaped(str(file))}.", err=True)
    for warning in warnings:
        typer.secho(_escaped(warning), fg="yellow", err=True)


def _not_in_a_file(kept: dict[str, Any]) -> list[str]:
    """The rules kept that a rules file does not carry (the Live Reviewer, auto mode's review), by their names."""
    from latet.transfer import KEYS

    return sorted(key for key in kept if key not in KEYS)


@rules_cli.command("forget", hidden=True)  # only a server an earlier version left has rules to forget
def shared_rules_forget(yes: Yes = False) -> None:
    """Delete the rules for everyone an earlier version kept in the shared settings (no space applies them): export
    them first to share them by hand."""
    from latet.server.shared import kept_said

    _mutation("PUT")
    if _whose.get("key") is not None:
        _stop("Forget applies only to the rules kept in the shared settings.")
    data, etag = _shared()
    if not (kept := _whose.get("kept")):
        typer.echo("The shared settings keep no rules for everyone: nothing to forget.")
        return
    left = _not_in_a_file(kept)
    carried = f" A rules file does not carry {', '.join(left)}." if left else ""
    _confirm(_escaped(f"Delete the rules for everyone kept on {_target_url()} ({kept_said(kept)})?{carried}"), yes)
    _ask("PUT", "shared", {**data, "etag": etag, "forget_kept_rules": True}, recovery="config get")
    typer.echo("The rules kept from an earlier version were deleted.")


def _when(text: str) -> float:
    """``7d``, ``12h``, ``30m``, or a date (`2026-11-01`, `2026-11-01T09:00`), as epoch seconds."""
    import datetime
    import re

    try:
        text = text.strip()
        if not text.isascii() or not 1 <= len(text) <= 64:
            raise ValueError
        if found := re.fullmatch(r"(\d+)([dhm])", text):
            seconds = int(found[1]) * {"d": 86400, "h": 3600, "m": 60}[found[2]]
            value = time.time() - seconds
        elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
            value = finite_time(text)
            assert value is not None
            return value
        else:
            value = datetime.datetime.fromisoformat(text).timestamp()
        if not math.isfinite(value):
            raise ValueError
        datetime.datetime.fromtimestamp(value)
        return value
    except (ValueError, OverflowError, OSError):
        raise typer.BadParameter("7d, 12h, 30m, or a date such as 2026-11-01") from None


def _log_page(
    found: dict[str, Any], filters: LogFilters, before: LogCursor | None, requested: int
) -> tuple[list[dict[str, Any]], LogCursor | None]:
    """Validate a whole cursor page before any of its entries may be emitted."""
    entries = found.get("entries")
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError("invalid log page")
    if len(entries) > requested:
        raise ValueError("invalid log page")
    if "next_cursor" not in found:
        raise ValueError("invalid log page")
    token = found["next_cursor"]
    after = None
    if token is not None:
        if not isinstance(token, str) or not entries:
            raise ValueError("invalid log page")
        after = LogCursor.decode(token, filters)
        if before is not None and (token == before.encode() or after.ceiling_id != before.ceiling_id):
            raise ValueError("invalid log page")
    ceiling = before.ceiling_id if before is not None else after.ceiling_id if after is not None else ID_MOST
    previous = (before.ts, before.id) if before is not None else None
    for entry in entries:
        if any(not isinstance(entry.get(key), str) for key in ("actor", "action", "prev", "hash")):
            raise ValueError("invalid log page")
        if any(
            key not in entry or (entry[key] is not None and not isinstance(entry[key], str))
            for key in ("target", "address")
        ):
            raise ValueError("invalid log page")
        if not isinstance(entry.get("details"), dict):
            raise ValueError("invalid log page")
        row, stamp = entry.get("id"), entry.get("ts")
        if type(row) is not int or not 1 <= row <= ceiling:
            raise ValueError("invalid log page")
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ValueError("invalid log page")
        if (filters.since is not None and stamp < filters.since) or (
            filters.until is not None and stamp >= filters.until
        ):
            raise ValueError("invalid log page")
        if filters.actor and entry.get("actor") != filters.actor:
            raise ValueError("invalid log page")
        if filters.action:
            selected = entry.get("action")
            if not isinstance(selected, str) or not (
                selected.startswith(filters.action[:-1]) if filters.action.endswith("*") else selected == filters.action
            ):
                raise ValueError("invalid log page")
        pair = (stamp, row)
        if previous is not None and pair >= previous:
            raise ValueError("invalid log page")
        previous = pair
    if after is not None and previous != (after.ts, after.id):
        raise ValueError("invalid log page")
    return entries, after


def _log_count(emitted: int) -> str:
    return f"{emitted} {'entry' if emitted == 1 else 'entries'} emitted"


def _log_incomplete(
    emitted: int, before: LogCursor | None, reason: str = "The server's log page is invalid."
) -> NoReturn:
    try:
        typer.echo(f"Retrieval is incomplete: {_log_count(emitted)}. {reason}", err=True)
        if before is not None:
            typer.echo(f"Resume cursor: {before.encode()}", err=True)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    raise typer.Exit(1)


def _log_render(
    entries: list[dict[str, Any]], as_json: bool, remaining: int, check: Callable[[], None]
) -> list[tuple[str, int]]:
    """Keep one page's escaped rendering only, refusing it whole before output if it cannot fit."""
    rendered = []
    for entry in entries:
        check()
        if as_json:
            text = json.dumps(entry, ensure_ascii=True, allow_nan=False) + "\n"
        else:
            details = entry.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            more = " ".join(
                f"{_text(k)}={_escaped(json.dumps(v, ensure_ascii=True, allow_nan=False))}" for k, v in details.items()
            )
            text = (
                f"{_seen(entry.get('ts'))}  {_text(entry.get('action'))}  "
                f"{_text(entry.get('target') or '-')} by {_text(entry.get('actor'))} "
                f"from {_text(entry.get('address') or '-')}  {more}"
            ).rstrip() + "\n"
            if entry.get("action") == "calls.refused":
                text += f"  {_count(details.get('count'))} calls refused: {_text(details.get('reason'))}\n"
        check()
        size = len(text.encode("utf-8"))
        if size > remaining:
            raise BufferError
        rendered.append((text, size))
        remaining -= size
    return rendered


log_cli = typer.Typer(
    help="The administration log: who did what, when, on what, from where (never a value); `verify` checks its chain.",
    invoke_without_command=True,
)
server_app.add_typer(log_cli, name="log")


@log_cli.callback()
def server_log(
    ctx: typer.Context,
    since: Annotated[str | None, typer.Option(help="From: 7d, 12h, or an ISO date.")] = None,
    until: Annotated[str | None, typer.Option(help="Until: excluded; accepts exact epoch seconds too.")] = None,
    action: Annotated[str | None, typer.Option(help="One action.")] = None,
    actor: Annotated[str | None, typer.Option(help="An identity id or admin:<token id>.")] = None,
    cursor: Annotated[str | None, typer.Option(help="Resume cursor, with the same original filters.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1_000_000, help="Maximum entries to read.")] = 100,
    as_json: Annotated[bool, typer.Option("--json", help="JSON lines, with exact id and ts.")] = False,
) -> None:
    """Who did what, when, on what, from where; values are never recorded."""
    if ctx.invoked_subcommand is not None:
        return
    from latet.server.client import ReceiveBudget

    end = _log_clock() + _LOG_SECONDS
    query: dict[str, Any] = {"limit": min(limit, LOG_MOST)}
    if since:
        query["since"] = _when(since)
    if until:
        query["until"] = _when(until)
    if action:
        query["action"] = action
    if actor:
        from latet.server.connection import USER_ID

        _not_secret(actor)
        if actor.startswith("admin:"):
            _id(actor.removeprefix("admin:"), ADMIN_TOKEN_ID)
        else:
            _id(actor, USER_ID)
        query["actor"] = actor
    if cursor is not None:
        query["cursor"] = cursor
    filters = LogFilters(query.get("since"), query.get("until"), action, actor)
    emitted = 0
    before = None
    if cursor is not None:
        try:
            before = LogCursor.decode(cursor, filters)
        except ValueError:
            raise typer.BadParameter("Not a valid log cursor for these original filters.") from None
    receive = ReceiveBudget(_LOG_RECEIVE_MOST)
    requests = rendered_bytes = 0

    def check() -> None:
        if _log_clock() > end:
            raise TimeoutError

    while emitted < limit:
        try:
            check()
            if requests >= _LOG_REQUEST_MOST:
                _log_incomplete(emitted, before, "The page-request budget is exhausted.")
            if receive.remaining <= 0:
                _log_incomplete(emitted, before, "The response-body receive budget is exhausted.")
            left = end - _log_clock()
            if left <= 0:
                raise TimeoutError
            query["limit"] = min(limit - emitted, LOG_MOST)
            requests += 1
            try:
                found = _ask(
                    "GET",
                    "log",
                    query=query,
                    limit=min(32 * 1024 * 1024, receive.remaining),
                    deadline=min(60.0, left),
                    receive=receive,
                    end=end,
                )
            except typer.Exit:
                _log_incomplete(emitted, before, "The server request could not be completed.")
            check()
            legacy = before is None and requests == 1 and "next_cursor" not in found
            if legacy:
                entries = _rows(found, "entries")
                if len(entries) > query["limit"]:
                    raise ValueError("invalid log page")
                after = None
            else:
                entries, after = _log_page(found, filters, before, query["limit"])
            check()
            rendered = _log_render(entries, as_json, _LOG_RENDER_MOST - rendered_bytes, check)
            text = ""
            for index, (text, size) in enumerate(rendered):
                check()
                typer.echo(text, nl=False)
                emitted += 1
                rendered_bytes += size
                if index + 1 == len(rendered):
                    before = after
                check()
            if legacy:
                typer.echo(
                    f"Legacy server: retrieval is incomplete; {_log_count(emitted)}. "
                    "Older entries may exist. This command reads one bounded page; "
                    "use --json for each exact ts and id. "
                    "Do not copy the plain output's rounded time into --until: it would skip entries in that second. "
                    "Exact continuation without skipping equal timestamps requires the server cursor.",
                    err=True,
                )
                return
            if after is None:
                typer.echo(f"{_log_count(emitted)}; no more matching rows were visible to these reads.", err=True)
                return
            if emitted >= limit:
                typer.echo(f"{_log_count(emitted)}; a server continuation remains.", err=True)
                typer.echo(f"Resume cursor: {after.encode()}", err=True)
                return
            query["cursor"] = after.encode()
            # Drop the previous page and its rendering before another response may be read.
            del found, entries, rendered, text
        except TimeoutError:
            _log_incomplete(emitted, before, "The command deadline was exceeded.")
        except BufferError:
            reason = "The rendered-output budget is exhausted."
            if emitted == 0:
                reason += " Request fewer rows."
            _log_incomplete(emitted, before, reason)
        except (BrokenPipeError, KeyboardInterrupt):
            _log_incomplete(emitted, before, "Output or retrieval was interrupted; the last entry may be partial.")
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
            _log_incomplete(emitted, before)


@log_cli.command("verify")
def log_verify() -> None:
    """Walk the log's chain and print its head for a copy kept elsewhere."""
    found = _ask("GET", "log/verify")
    head = found.get("head")
    if isinstance(head, dict):
        typer.echo(f"Head: {_count(head.get('id'))} {_text(head.get('hash'))}")
    else:
        typer.echo("Head: " + _escaped(json.dumps(head, ensure_ascii=True)))
    typer.echo(
        "Entries taken off the log's end leave its chain whole: compare this head "
        "with the one noted last time, or E4's copy."
    )
    if found.get("ok") is True:
        typer.echo(f"The administration log's chain is whole: {_count(found.get('checked'))} entries verified.")
        return
    broken = found.get("broken_at")
    if broken is None:
        _stop("The administration log's chain could not be walked.", 1)
    _stop(
        f"The administration log's chain is broken at entry {_count(broken)}: "
        f"{_count(found.get('checked'))} entries verified before it.",
        1,
    )


@token_cli.command("issue")
def token_issue(
    label: Annotated[str | None, typer.Option(help="Whose or what it is for; required with --never.")] = None,
    days: Annotated[
        int | None, typer.Option(min=1, max=3650, help="Days it lasts; default: server's admin_token_days.")
    ] = None,
    never: Annotated[
        bool,
        typer.Option(
            "--never",
            help=(
                "Never expires; can do all an administrator can until revoked. "
                "Only a lasting token can issue one. Keep it apart to issue others. Give CI a token with --days."
            ),
        ),
    ] = False,
    out: Annotated[Path | None, typer.Option(help="Write a private {url, token} record; print no secret.")] = None,
) -> None:
    """Issue an administration token once; --out keeps its secret out of a script's log."""
    if days is not None and never:
        _stop("--days or --never, not both")
    if never and not label:
        _stop("--never needs --label")
    if label is not None:
        _no_secret_in(label)
    _check_out(out)
    body: dict[str, Any] = {"label": label or "administration"}
    if never:
        body["never"] = True
    elif days is not None:
        body["days"] = days
    before = time.time()
    answer = _ask("POST", "admin-tokens", body, recovery="admin-token list")
    token_id, secret = answer.get("id"), answer.get("token")
    if (
        not isinstance(token_id, str)
        or not ADMIN_TOKEN_ID.fullmatch(token_id)
        or not isinstance(secret, str)
        or not ADMIN_TOKEN.fullmatch(secret)
    ):
        _stop("the server's answer is not an administration token", 1)
    if out is not None:
        _private_write(out, json.dumps({"url": _target_url(), "token": secret}) + "\n")
        typer.echo(f"{token_id}: administration token in {_escaped(str(out))}")
        return
    typer.echo(f"An administration token ({token_id}), shown once: keep it in a password manager.")
    typer.echo(f"  {secret}")
    expires = answer.get("expires")
    typer.echo("It never expires." if expires is None else f"It expires on {_seen(expires)}.")
    typer.echo("An issued administration token cannot outlast its issuer.")
    if never:
        typer.echo(
            "It can do all an administrator can: people and their one-time codes, API "
            "keys, shared settings and administration tokens,"
        )
        typer.echo(f"until latet server admin-token revoke {token_id}. Keep it apart, used only to issue others.")
        typer.echo(
            "Only a token that never expires issues one, or latet server init "
            "--new-admin-token --never on the server's machine."
        )
        typer.echo(
            "A CI holding it gives all that, for good, to whoever can change its "
            "jobs: give CI --days and issue again before it ends."
        )
    elif (
        days is not None
        and isinstance(expires, (int, float))
        and not isinstance(expires, bool)
        and expires < before + days * 86400 - 60
    ):
        typer.echo("The server may have cut this token at the issuing token's end, earlier than the requested days.")


@token_cli.command("list")
def token_list() -> None:
    """Every token: this marks the one in use; tampered rows open nothing; never a secret."""
    for token in _rows(_ask("GET", "admin-tokens"), "tokens"):
        token_id = token.get("id")
        shown = token_id if isinstance(token_id, str) and ADMIN_TOKEN_ID.fullmatch(token_id) else "?"
        marker = " this" if token.get("current") is True or token.get("this") is True else ""
        expires = "never" if token.get("expires") is None else _seen(token.get("expires"))
        typer.echo(
            f"{shown}{marker}  {_text(token.get('label'))}  made "
            f"{_seen(token.get('created'))}  last used {_seen(token.get('last_used'))} "
            f" expires {expires}  issued by {_text(token.get('issued_by') or token.get('issuer') or '-')}"
        )
        if token.get("tampered") is True:
            typer.echo("  written without the server's key: it opens nothing")


@token_cli.command("revoke")
def token_revoke(
    token_id: Annotated[str, typer.Argument(help="The administration token id, never its secret.")],
    yes: Yes = False,
) -> None:
    """Revoke a token; revoking this or the last one asks and explains the way back."""
    _id(token_id, ADMIN_TOKEN_ID)
    tokens = _rows(_ask("GET", "admin-tokens"), "tokens")
    found = next((token for token in tokens if token.get("id") == token_id), None)
    working = [token for token in tokens if token.get("tampered") is not True]  # a tampered row opens nothing
    if found and (found.get("current") is True or found.get("this") is True or working == [found]):
        _confirm(
            f"Revoke {token_id} on {_target_url()}? This is the token in use or the last one. "
            "The way back is latet server init --new-admin-token on the server's machine.",
            yes,
        )
    answer = _ask("DELETE", f"admin-tokens/{token_id}", recovery="admin-token list")
    if not answer.get("revoked"):
        _stop("Nothing was revoked: no administration token has that id.", 1)
    typer.echo(f"{token_id} revoked: the change reaches every instance within seconds.")
