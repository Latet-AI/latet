"""A Latet server's instance on this machine: its settings, its master key, the database's tables, its first
administration token, its uvicorn server, and whether it answers ready. What `latet server init|migrate|run|ready` and
the Windows service use. It needs the `server` extra (psycopg, its pool, redis); a computer's install imports it only
to be told so (``require_extra``).

The master key is written only in a folder that is the person's alone (``storage.folders.private_folder_problem``, as
a computer's token and a server's TLS key are): on Windows a file takes its folder's access list, and the master key
opens every vault, makes administration tokens and the administration log's chain. ``migrate_schema`` loads it before
it changes anything and checks it against the database (the check value an instance's first start seals): an entry
written under another key would break the chain. The first administration token is issued once: a token the server
lists is one issued before, whatever the log still holds (it is pruned past its retention), and the token and its
entries are written in one transaction. What the database's driver raises is said by its type, never its words (libpq's
name hosts and users).

An instance (``make_instance``) logs as the settings say from its first line, opens the server (which refuses a schema
that is not the code's, or a master key that is not the database's), and loads its certificate and key before it runs,
so that what stops it is said in fixed words. It binds its sockets itself: its own on the settings' host and port alone,
and with metrics on the metrics socket on metrics_host and metrics_port (E0c's T10.12), both served by one uvicorn
server behind E0c's operations, which answer the metrics on that socket alone; a socket it cannot bind stops it in
fixed words naming its settings. Its run (``Instance.run``) closes the server whatever ends it, a stop
signal included: uvicorn shuts down gracefully on SIGTERM or SIGINT and then raises the signal again, which with the
default disposition would end the process before the server is closed and the log written out."""

from __future__ import annotations

import errno
import gc
import hashlib
import http.client
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, LiteralString, TextIO
from urllib.parse import urlsplit

from latet import __version__
from latet.config import write_atomic
from latet.logline import described
from latet.storage import folders

if TYPE_CHECKING:
    from uvicorn import Server as Uvicorn

    from latet.server.app import Server
    from latet.server.db import Database
    from latet.server.settings import ServerSettings

log = logging.getLogger(__name__)

EXTRA = 'the `server` extra: uv tool install --force "latet[server] @ <the release\'s wheel>"'
# How long a stop waits for what is under way (a rolling restart): a stream that lasts longer is cut.
GRACEFUL_SECONDS = 30
_MIB = 1024 * 1024
# Where this machine's instance is asked when it listens on every address: there, on the loopback of the same family
# (asyncio listens on IPv6 alone for ::).
_HERE = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}  # noqa: S104 - read, not a bind
_TWICE = (
    "the master key is given twice, by LATET_MASTER_KEY and by LATET_MASTER_KEY_FILE (master_key_file): give it once"
)
_STARTED = (
    "this database was started with another master key, and a new one would open nothing it seals: give that key, in "
    "the file master_key_file names (LATET_MASTER_KEY_FILE) or in LATET_MASTER_KEY"
)
# Why no instance runs in asyncio's debug mode: `latet server run` says it before anything, and make_instance refuses.
DEBUG_MODE = (
    "asyncio's debug mode is on (-X dev, PYTHONDEVMODE or PYTHONASYNCIODEBUG), and its lines name callbacks with what "
    "they hold, a value or a secret among them: start the server without -X dev, PYTHONDEVMODE and PYTHONASYNCIODEBUG"
)
# The settings that tell an instance to trust what it reaches without checking it, each with what it then trusts.
_TRUSTED = (
    (
        "database_private_network",
        "PostgreSQL is reached with no TLS that checks it: only the instances and PostgreSQL must share that network",
    ),
    (
        "valkey_private_network",
        "Valkey is reached with no TLS that checks it: only the instances and Valkey must share that network",
    ),
    ("valkey_unauthenticated", "Valkey is reached with no password: nothing but the instances must reach it"),
)
_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name))
# The instance's two sockets, as a failure to bind one names them: what, and the settings that place it.
_OWN = ("the instance's socket", "host and port (LATET_SERVER_HOST, LATET_SERVER_PORT)")
_METRICS = (
    "the metrics socket",
    "metrics_host and metrics_port (LATET_SERVER_METRICS_HOST, LATET_SERVER_METRICS_PORT)",
)
# The errors of a bind, as the instance says them: an address and a port taken, or kept from this account.
_IN_USE = frozenset({errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)})
_FORBIDDEN = frozenset({errno.EACCES, getattr(errno, "WSAEACCES", errno.EACCES)})


class NeedsExtra(RuntimeError):
    """This Latet was installed without the `server` extra."""


class CannotListen(RuntimeError):
    """A socket of the instance, its own or the metrics', cannot be bound: the instance does not start. Its words name
    the settings and why, never the address."""


def require_extra() -> None:
    try:
        import psycopg  # noqa: F401
        import psycopg_pool  # noqa: F401
        import redis  # noqa: F401
    except ImportError:
        raise NeedsExtra(f"A Latet server needs {EXTRA}") from None


def settings(config: Path | None = None) -> ServerSettings:
    """The server's settings: its LATET_SERVER_* variables over ``config`` (a read-only server.yaml)."""
    from latet.server.settings import load_server_settings

    return load_server_settings(config)


def asyncio_debug() -> bool:
    """Whether asyncio would run in its debug mode, whose lines name callbacks and coroutines with what they hold (a
    value, a token, a body): Python's development mode (-X dev, PYTHONDEVMODE), or PYTHONASYNCIODEBUG."""
    return bool(sys.flags.dev_mode or os.environ.get("PYTHONASYNCIODEBUG"))


def ensure_master_key(settings: ServerSettings, database: Database | None = None) -> Path | str | None:
    """The master key for `latet server init`: None when there is one (LATET_MASTER_KEY, or the file master_key_file
    names); the file, written now, when master_key_file names one that is not there; a new key to keep when nothing
    names one (nothing is written then). A key is 32 random bytes in base64 (E0b's ``MasterKey.generate``).

    No key is made for a database an instance started on (``database``, else the settings'): it holds the check value
    the key it was started with sealed, which a new key opens nothing of. MasterKeyError then, in words that say how to
    give that key, nothing written and nothing printed. The file is written only in a folder that is the person's
    alone, made so (0700, Python's private folder on Windows) when it is not there: raises PermissionError otherwise,
    in words that name the folder and the fix and hold no key, and nothing is written. Raises MasterKeyError, before
    anything, when both LATET_MASTER_KEY and master_key_file are given, and DatabaseError when the database cannot be
    asked."""
    from latet.server.master import MasterKey, MasterKeyError

    path = settings.master_key_file
    given = bool(os.environ.get("LATET_MASTER_KEY", "").strip())
    if given and path is not None:  # which of the two is the key is not guessed, as MasterKey.load does not
        raise MasterKeyError(_TWICE)
    if given or (path is not None and path.exists()):
        return None
    db, mine = _database(settings, database)
    try:
        if _sealed(db):
            raise MasterKeyError(_STARTED)
    finally:
        if mine:
            db.close()
    if path is None:
        return MasterKey.generate()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (problem := folders.private_folder_problem(path.parent)) is not None:
        raise PermissionError(f"the master key is not written: {problem}")
    write_atomic(path, MasterKey.generate() + "\n", private=True)
    return path


def _database(settings: ServerSettings, database: Database | None) -> tuple[Database, bool]:
    from latet.server.db import Database

    if database is not None:
        return database, False
    db = Database(
        settings.database_url,
        schema="latet",
        connect_timeout=settings.database_connect_timeout,
        max_size=settings.database_pool,
    )
    return db, True


def _asked[T](db: Database, query: LiteralString, read: Callable[[Any], T]) -> T:
    """``read`` of what ``query`` answers on ``db``. Raises DatabaseError, by the type of the driver's error."""
    import psycopg

    from latet.server.db import DatabaseError

    kind = ""
    try:
        with db.connection() as conn:
            return read(conn.execute(query).fetchone())
    except psycopg.Error as exc:
        kind = type(exc).__name__
    raise DatabaseError(f"the database could not be asked for the master key's check value ({kind})")


def _keeps_a_check(db: Database) -> bool:
    """Whether the database has the table of the master key's check value (an instance started on it, or a migration
    made it). Raises DatabaseError, by the type of the driver's error."""
    return _asked(db, "SELECT to_regclass('master_check')", lambda row: row is not None and row[0] is not None)


def _sealed(db: Database) -> bool:
    """Whether the database holds the master key's check value: an instance started on it, with the key that sealed
    it. Raises DatabaseError, by the type of the driver's error."""
    return _keeps_a_check(db) and _asked(db, "SELECT 1 FROM master_check WHERE id = 1", lambda row: row is not None)


def migrate_schema(settings: ServerSettings, database: Database | None = None) -> list[str]:
    """The database's tables brought to the code's version, one machine at a time (E0b's lock): the migrations applied
    now. The master key is loaded before anything is changed, and checked against the database's check value before the
    migrations when the database has the table that keeps it, right after them otherwise (a database's first start,
    which seals the value with this key): a key that does not open the database changes nothing, since an entry written
    with it would break the administration log's chain. What was applied is recorded there, under the key the instances
    derive. Raises MasterKeyError, DatabaseError (the driver's error by its type) or ValueError, in fixed words."""
    import psycopg

    from latet.server.audit import AuditLog
    from latet.server.db import DatabaseError, migrate
    from latet.server.master import MasterKey, check_master_key

    master = MasterKey.load(settings)
    db, mine = _database(settings, database)
    try:
        checked = _keeps_a_check(db)
        if checked:
            check_master_key(db, master)
        applied = migrate(db)
        if not checked:
            check_master_key(db, master)
        kind = ""
        if applied:
            try:
                audit = AuditLog(db, key=master.derive(b"audit"), clock=time.time)
                audit.record("server", "server.migrated", None, {"migrations": applied})
            except psycopg.Error as exc:
                kind = type(exc).__name__
        if kind:
            raise DatabaseError(f"the migrations were applied, and their entry could not be written ({kind})")
        return applied
    finally:
        if mine:
            db.close()


def schema_state(settings: ServerSettings, database: Database | None = None) -> str | None:
    """None when the database's tables are the code's version; else why not (E0b's words name the command). Raises
    DatabaseError when PostgreSQL cannot be asked."""
    from latet.server.db import SchemaMismatch, check_schema

    db, mine = _database(settings, database)
    try:
        check_schema(db)
        return None
    except SchemaMismatch as exc:
        return str(exc)
    finally:
        if mine:
            db.close()


def _init_lock(schema: str) -> int:
    """The advisory lock of a schema's first administration token (``first_admin_token``)."""
    return int.from_bytes(hashlib.sha256(f"latet-init:{schema}".encode()).digest()[:8], "big", signed=True)


def first_admin_token(
    server: Server, *, again: bool = False, days: int = 0, never: bool = False
) -> tuple[str, str] | None:
    """An administration token for `latet server init`, its id and its secret (shown once); None when one was issued
    before (a token the server lists: the log is pruned past its retention, and a token issued otherwise left no
    entry), unless ``again`` (an administrator who lost theirs, on a machine with the database's access and the master
    key). It lasts ``days`` (0: the server's admin_token_days) or, with ``never``, for good: the one origin of a token
    for good, as no token issues one that ends after itself. The token, ``server.initialized`` (the first) and
    ``admin-token.issued`` (actor ``server``, target the token's id, ``{"again": true}`` or nothing: never a label) are
    written in one transaction, which reads the token list under a lock of its own, so that two inits run at once on a
    fresh database (two containers, two machines) issue one first token. Raises DatabaseError, by the type of the
    driver's error."""
    import psycopg

    from latet.server.db import DatabaseError

    kind = ""
    try:
        label = "issued again by latet server init" if again else "the first, by latet server init"
        with server.db.transaction() as conn:
            if not again:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_init_lock(server.db.schema),))
                if server.admin.list(conn=conn):
                    return None
            token_id, secret, _ = server.admin.issued(label, days=days, never=never, by="server", conn=conn)
            if not again:
                server.audit.record("server", "server.initialized", None, {"url": server.settings.url}, conn=conn)
            server.audit.record("server", "admin-token.issued", token_id, {"again": True} if again else {}, conn=conn)
        return token_id, secret
    except psycopg.Error as exc:
        kind = type(exc).__name__
    raise DatabaseError(f"the administration token could not be issued ({kind})")


class _Stopped(BaseException):
    """A stop signal, which uvicorn raises again once it has stopped gracefully: ``Instance.run`` takes it."""


def _stop(signum: int, frame: Any) -> None:
    raise _Stopped


def _stopping_on_signals() -> dict[int, Any]:
    """In the main thread (where signals are heard), each stop signal ending the run instead of the process: the
    handlers it had before, for ``_given_back``. Elsewhere (the Windows service's thread), none."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {sig: signal.signal(sig, _stop) for sig in _SIGNALS}


def _given_back(before: dict[int, Any]) -> None:
    for sig, handler in before.items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)  # None: not set from Python


def _written_out() -> None:
    """What the log counted and holds, written out: the process may end next."""
    from latet.server import logs

    logs.flush()
    for handler in logging.getLogger().handlers:
        handler.flush()


@dataclass(eq=False)
class Instance:
    """This machine's instance (``make_instance``): the server it opened, the uvicorn server that serves its app, and
    the sockets it serves (``own``: bound on the settings' host and port; ``metrics``: on metrics_host and metrics_port,
    with metrics on). One uvicorn server serves them all, E0c's operations outermost, which tell them apart by the port
    a request came to (T10.12). ``own`` None: uvicorn binds the settings' host and port itself (an instance made
    otherwise, in a test)."""

    server: Server
    uvicorn: Uvicorn
    own: list[socket.socket] | None = None
    metrics: list[socket.socket] = field(default_factory=list)

    def run(self) -> None:
        """Serve until stopped, then close the server whatever ended the run, and write out what the log holds. A stop
        signal (SIGTERM, as Docker, systemd and Kubernetes stop a server; SIGINT, Ctrl-C; SIGBREAK on Windows) ends it
        gracefully, uvicorn waiting ``GRACEFUL_SECONDS`` at most for what is under way, and it returns. Signals are
        heard in the main thread: run from another (the Windows service's), it ends when ``uvicorn.should_exit`` is
        set. The sockets are let go whatever ended the run (uvicorn closes them once it served; a start that failed did
        not)."""
        settings = self.server.settings
        over = "over https (TLS at the instance)" if settings.certificate is not None else "over http (TLS before it)"
        if self.metrics:
            log.info(
                "instance %s of Latet %s starts on %s port %d, %s; its metrics on %s port %d",
                self.server.instance, __version__, settings.host, settings.port, over, settings.metrics_host,
                settings.metrics_port,
            )  # fmt: skip
        else:
            log.info(
                "instance %s of Latet %s starts on %s port %d, %s; no metrics socket (metrics_token_file is not set)",
                self.server.instance, __version__, settings.host, settings.port, over,
            )  # fmt: skip
        sockets = None if self.own is None else [*self.own, *self.metrics]
        # What the instance opened and its app, kept for good: no full collection walks them again while it runs, which
        # halved the event loop's worst gap while vaults were read (measured: 71-81 ms to 34-39 ms for ten of 50,000).
        gc.collect()
        gc.freeze()
        before = _stopping_on_signals()
        try:
            self.uvicorn.run(sockets=sockets)
        except _Stopped:
            pass  # stopped as asked
        finally:
            _given_back(before)
            gc.unfreeze()
            for sock in sockets or []:
                sock.close()
            try:
                self.server.close()
            finally:
                _written_out()


def _loaded(made: Uvicorn) -> None:
    """uvicorn's configuration loaded now, the instance's certificate and key with it, rather than once its run has
    begun: a file that is not there, cannot be read, is no certificate or is not its key stops the start in fixed words
    (a ValueError naming the settings and the error's type, never what the file holds)."""
    kind = ""
    try:
        made.config.load()
    except OSError as exc:  # ssl.SSLError is one
        kind = type(exc).__name__
    if kind:
        raise ValueError(
            f"the instance's certificate and key (certificate, key) cannot be used ({kind}): each is a PEM file this "
            "account can read, the key the certificate's own"
        )


def _why_not(error: OSError | UnicodeError) -> str:
    """Why a socket cannot be bound, in words of its own: its address and port in use, kept from this account, or
    unknown here; else the error by its type (``described``: never its words, which name the address)."""
    if isinstance(error, socket.gaierror | UnicodeError):  # the second: a host no name can be (an empty label)
        return "its host is no name or address this machine knows"
    if error.errno in _IN_USE or getattr(error, "winerror", None) in _IN_USE:
        return "its address and port are in use: another program listens there"
    if error.errno in _FORBIDDEN or getattr(error, "winerror", None) in _FORBIDDEN:
        return "this account may not listen on its port, or the system keeps it"
    return described(error)


def _listening(host: str, port: int, which: tuple[str, str]) -> list[socket.socket]:
    """Sockets bound on ``host`` and ``port`` as asyncio binds a server's: one for each address the host is resolved to
    (``AI_PASSIVE``; an empty host, every address of this machine), an IPv6 one on IPv6 alone, an address whose family
    this machine has not enabled skipped, ``SO_REUSEADDR`` on POSIX alone, and ``SO_EXCLUSIVEADDRUSE`` on Windows, where
    another socket could otherwise bind the same address and port with ``SO_REUSEADDR``. uvicorn's server makes them
    listen when it starts. Raises CannotListen, in fixed words naming ``which`` (what, and its settings), every socket
    made closed first."""
    found: list[socket.socket] = []
    error: OSError | UnicodeError | None = None
    try:
        infos = socket.getaddrinfo(host or None, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
        for family, kind, proto, _, address in dict.fromkeys(infos):  # each once, in their order
            try:
                sock = socket.socket(family, kind, proto)
            except OSError:  # a family or a protocol this machine has not: asyncio goes on too
                continue
            found.append(sock)
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif sys.platform != "cygwin":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind(address)
            except OSError as exc:
                if exc.errno != errno.EADDRNOTAVAIL:  # a family not enabled here is skipped, as asyncio skips it
                    raise
                found.remove(sock)
                sock.close()
        if not found:
            raise OSError(errno.EADDRNOTAVAIL, "no address")
    except (OSError, UnicodeError) as exc:  # its words name the address: said in words of the instance's own, below
        error = exc
    if error is not None:
        for sock in found:
            sock.close()
        what, settings = which
        raise CannotListen(f"{what} cannot be bound on {settings}: {_why_not(error)}")
    return found


def make_instance(settings: ServerSettings, *, stream: TextIO | None = None) -> Instance:
    """This machine's instance, ready to run: logging set up (to the standard output, or ``stream``: the Windows
    service's file) and the settings that trust a network or a Valkey said, the server opened (it refuses a schema that
    is not the code's, or a master key that is not the database's), E0c's operations in front of its app, HTTPS when the
    settings name a certificate (for both sockets), the proxies' addresses the only ones believed, a WebSocket message
    no larger than a body, uvicorn's own logging left to the server's, and its sockets bound last (``_listening``): its
    own, and the metrics' only with metrics on. The server it opened, and any socket bound, are closed when a later step
    fails: CannotListen for a socket that cannot be bound. Raises RuntimeError (``DEBUG_MODE``) before anything in
    asyncio's debug mode, whoever makes it: `latet server run` says so first, and the Windows service's host calls this
    itself (Task 14)."""
    from latet.cli.main import uvicorn_server
    from latet.server.app import open_server
    from latet.server.logs import configure_logging
    from latet.server.operations import server_app

    if asyncio_debug():
        raise RuntimeError(DEBUG_MODE)
    configure_logging(settings.log_format, settings.log_level, stream=stream)
    for name, trusts in _TRUSTED:
        if getattr(settings, name):
            log.warning("%s is on: %s", name, trusts)
    server = open_server(settings)
    own: list[socket.socket] = []
    metrics: list[socket.socket] = []
    try:
        tls = (
            (settings.certificate, settings.key)
            if settings.certificate is not None and settings.key is not None
            else None
        )
        made = uvicorn_server(
            server_app(server), settings.host, settings.port, None, log_config=None, tls=tls,
            forwarded_allow_ips=settings.forwarded_allow_ips,  # the proxy's word, from its address alone
            ws_max_size=settings.max_request_mb * _MIB, timeout_graceful_shutdown=GRACEFUL_SECONDS, access_log=False,
        )  # fmt: skip
        _loaded(made)
        # Bound last, by the instance itself (T10.12): its own socket on the settings' port alone, and the metrics' on
        # theirs only with metrics on, so that the metrics, which the operations answer on that port alone, are never
        # where a load balancer sends the instance's traffic.
        own = _listening(settings.host, settings.port, _OWN)
        if settings.metrics_token_file is not None:
            metrics = _listening(settings.metrics_host, settings.metrics_port, _METRICS)
    except BaseException:
        for sock in [*own, *metrics]:
            sock.close()
        server.close()
        raise
    return Instance(server, made, own, metrics)


def _own(pem: str) -> tuple[str, bytes]:
    """The first certificate of ``pem``, the one an instance presents as its own (a file of its chain holds the
    intermediates after it), as PEM and as DER. Raises ValueError when there is none."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    found = x509.load_pem_x509_certificates(pem.encode())[0]
    return found.public_bytes(serialization.Encoding.PEM).decode(), found.public_bytes(serialization.Encoding.DER)


def ready(settings: ServerSettings, timeout: float = 5.0) -> tuple[bool, str]:
    """Whether this machine's instance answers `/_latet/ready` (PostgreSQL and Valkey answer it, and it hears Valkey),
    asked on this machine with the server's own name; and why not, which the answer never says (its log does). Over
    HTTPS the instance's own certificate is pinned as T3.7 pins one: never an authority (CA:TRUE, or no CA:FALSE),
    whose key would sign for anyone, and the answer taken only when the certificate the instance presents is that one,
    byte for byte."""
    from latet.engine.reviewer import certificate as certificates
    from latet.server.certificate import AUTHORITY

    host, port = _HERE.get(settings.host, settings.host), settings.port
    conn: http.client.HTTPConnection
    own: bytes | None = None
    try:
        if settings.certificate is not None:
            pem, own = _own(settings.certificate.read_text("utf-8"))
            if certificates.is_authority(pem):
                return False, f"the instance's certificate (certificate) {AUTHORITY}"
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=certificates.context(pem))
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
    except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError; a file that is no text, a ValueError
        return False, f"the instance's certificate (certificate) cannot be read ({type(exc).__name__})"
    try:
        if own is not None:  # the handshake, then the certificate it presented, before anything is asked
            conn.connect()
            sock = conn.sock
            if not isinstance(sock, ssl.SSLSocket) or sock.getpeercert(binary_form=True) != own:
                return False, f"{host}:{port} does not present the certificate the settings name (certificate)"
        conn.request("GET", "/_latet/ready", headers={"Host": urlsplit(settings.url).netloc})
        answer = conn.getresponse()
        answer.read(64 * 1024)
        status = answer.status
    except ssl.SSLError as exc:  # another certificate than the settings', or no TLS there
        return False, f"{host}:{port} does not answer with the certificate the settings name ({type(exc).__name__})"
    except (OSError, http.client.HTTPException) as exc:
        return False, f"nothing answers on {host}:{port} ({type(exc).__name__})"
    finally:
        conn.close()
    if status == 200:
        return True, "ready"
    return False, f"not ready ({status}): the instance's log says which of PostgreSQL, Valkey or its ear it waits for"
