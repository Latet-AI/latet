"""PostgreSQL for a Latet server: a pool of connections in the server's schema; the migrations, SQL files applied in
order under an advisory lock, one instance at a time; and the check an instance makes before it starts.

The server's address is the whole of where it connects and as whom, and of how: libpq would fill in what an address
leaves out from the environment (PGHOST, PGUSER, PGSSLMODE...), from a password file and a client certificate in the
home folder, and from a service file, and none of it is let in. Every option that libpq has a variable for is given in
the connection's parameters (``_parameters``). An error of the driver is shown and logged by its type and never by its
words, which name hosts and users and, for an address libpq cannot read, quote it with its password.

Nor does the server rest on what a role or a database defaults to for a session. Every connection starts with the
search path (the schema alone) and with ``_PINNED``: all the digits of a DOUBLE PRECISION (what is read back is what
was written, which the hashes and tags over times need; a role at 0 gives 15 digits), READ COMMITTED as the isolation
of a transaction that asks for none (the administration log's chain reads the last hash after it waits for a lock,
which a stricter transaction reads from before the wait), UTF8 as the client's encoding (a role's LATIN1 would refuse
a name of another script on its way in; an address's own is set aside too), and ``STATEMENT_SECONDS`` as the longest a
statement runs. The start checks that they took effect, as it checks the search path (a pooler that drops the
start-up options is refused), and that the database's encoding is UTF8 (a name, a label or an entry's text from any
script is kept only there).

A connection that stops answering once it is open is given up: TCP's keepalives find a peer that went silent while
the connection waits, and what was sent and not acknowledged gives it up (``_BOUNDS``, which libpq has no variable for,
and which an address does not set aside), and the statement bound ends one a live PostgreSQL holds. A peer whose system
still acknowledges what it gets (a host whose PostgreSQL hangs, a relay that stops passing anything on) is bounded by
none of them: a call that has waited ``ANSWER_SECONDS`` for its answer fails, its connection closed
(``_Connection.wait``). The migrations lift both bounds for their own connection, and wait ``MIGRATION_WAIT`` at most
for another instance's lock."""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from itertools import zip_longest
from typing import Any, Self
from urllib.parse import unquote

import psycopg
from psycopg import pq, sql
from psycopg.abc import PQGen
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

# The packages of the migrations, in order: each holds NNN_name.sql files. E1 appends "latet.ee.migrations" when the
# Enterprise package can be imported.
MIGRATIONS: list[str] = ["latet.server.migrations"]
# Seconds PostgreSQL has to answer a new connection before it is given up (the setting database_connect_timeout). Left
# to the environment it would be none, and psycopg then waits 130 seconds for a database that does not answer.
CONNECT_TIMEOUT = 5
_TIMEOUTS = range(2, 121)  # libpq takes 1 for 2, and psycopg takes 0 for its 130
_SCHEMA = re.compile(r"[a-z][a-z0-9_]{0,62}")  # a name that is no list, no quoted text and no option of the connection
_SHARED = ("public", "information_schema")  # schemas that are not a server's own (and pg_* are the system's)
# The options that fall back to a file in the home folder when the address names none, or names an empty one: the
# password file, the client certificate and its key, the root certificate, the revocation list. Each is given a path
# that is not there, which libpq reads as "none". (Not ``sslcrldir``: it has no file in the home folder, and a
# directory that is not there makes OpenSSL fail every certificate it checks, as a PostgreSQL with TLS showed.)
_HOME_FILES = ("passfile", "sslcert", "sslkey", "sslrootcert", "sslcrl")
# On Windows "/latet-no-such-file" is a folder in the root of the system drive, which any local user may make, and put a
# password file or a root certificate in. A question mark is a character that Windows refuses in a file name, so no file
# of that name can be made (on Linux only root can make a folder in /); libpq reads the path as not there, as before.
_NO_FILE = "/latet-no-such-file/none?"
# Options that have no compiled default and that libpq refuses to read as empty: the value they have when nothing sets
# them (``sslcertmode`` allows a client certificate, the protocol versions are 3.0).
_UNSET = {"sslcertmode": "allow", "min_protocol_version": "3.0", "max_protocol_version": "3.0"}
# Options whose variable no explicit value sets aside: an empty ``require_auth`` allows no method at all, and an empty
# ``service`` names a service that is not found. Their variables are refused instead (``service`` and PGSERVICE
# always: a service file would fill in the rest).
_NO_VALUE = ("service", "require_auth")
# The names that libpq writes into ``sslmode``, the last parameter written winning: ``requiressl`` (``1`` is require,
# any other value prefer) and, in a URI, ``ssl=true`` (require). The sslmode of an address that holds one is not the
# one written, and the address is refused for it (see ``_has_sslmode_alias``).
_SSLMODE_ALIASES = ("ssl", "requiressl")
_URI_STARTS = ("postgresql://", "postgres://")  # a URI to libpq: these, in small letters
_BLANKS = " \t\n\v\f\r"  # what libpq's isspace() takes for a blank, which separates the parameters of an address
# Seconds a statement of the server's connections runs at most: the longest of its own (a history's cut at its limit,
# a removed identity's rows) takes seconds. The migrations lift it for their connection.
STATEMENT_SECONDS = 60
# Seconds a call waits for what PostgreSQL answers, whatever it asked: past them its connection is closed and it fails
# (OperationalError). PostgreSQL ends a statement past STATEMENT_SECONDS itself, and says so first; this bound is for a
# PostgreSQL that answers nothing while its host acknowledges what it is sent, which neither TCP's keepalives nor the
# user timeout see. The migrations lift it for their connection.
ANSWER_SECONDS: float = STATEMENT_SECONDS + 15
# What psycopg raises when one of its waits passes the time it was given (a name of its own, read here: a psycopg that
# renamed it fails the import, not a call).
_WAITED_TOO_LONG: type[Exception] = psycopg.errors._WaitTimeout
# Seconds an instance waits for the lock of the migrations that another holds (migrating, or stopped while it held it:
# the lock goes with its connection), before it gives up and says so.
MIGRATION_WAIT = 600
# The settings of a session that the server rests on, given to every connection in its start-up options whatever a
# role or a database says, and checked at start (as PostgreSQL shows them: the statement bound in milliseconds): see the
# module's words. The client's encoding is also given as libpq's own option, which an address's would win over.
_PINNED = {
    "extra_float_digits": "3",
    "default_transaction_isolation": "read committed",
    "client_encoding": "UTF8",
    "statement_timeout": str(STATEMENT_SECONDS * 1000),
}
# What gives up a connection that went silent once open (see the module's words), whatever the address says: TCP's
# keepalives after 10 seconds without a word, a probe every 5, three missed; what was sent and not acknowledged after
# 20 seconds (where the system has TCP_USER_TIMEOUT).
_BOUNDS = {
    "keepalives": "1",
    "keepalives_idle": "10",
    "keepalives_interval": "5",
    "keepalives_count": "3",
    "tcp_user_timeout": "20000",
}
_BACKSLASH = chr(0x5C)  # in the options of a connection a space inside a value is written with a backslash before it


class SchemaMismatch(RuntimeError):
    """The database's schema is not this Latet's: an instance refuses to start, and says what to run."""


class DatabaseError(RuntimeError):
    """PostgreSQL could not be reached, or refused or failed what a start asked of it. Its words name the driver's error
    by its type and never quote it: libpq's words hold hosts and users, and an address it cannot read."""


def _places(found: dict[str, Any]) -> list[tuple[str, str]]:
    """``(host, hostaddr)`` of each place in the address' list of hosts (libpq refuses lists of unequal length)."""
    hosts, addresses = str(found.get("host", "")).split(","), str(found.get("hostaddr", "")).split(",")
    return list(zip_longest(hosts, addresses, fillvalue=""))


def _hosts_named(found: dict[str, Any]) -> bool:
    """Whether each place in the address' list of hosts names a host or an address: an empty one is the default host,
    which libpq takes from PGHOST or from its socket directory."""
    return all(host or address for host, address in _places(found))


def _uri_query(uri: str, start: int) -> str:
    """The parameters of a URI, found as libpq finds them: what follows the first ``?`` that comes after the user
    information, the hosts and the database. The user information ends at the first ``@`` that no ``/`` comes before
    and may hold a ``?``; the hosts are separated by commas, one in brackets runs to the closing bracket, and the
    database runs to the first ``?``. ``start``: where the user information begins. An address libpq cannot read has
    no parameters here, it is refused before this is asked."""
    size, place = len(uri), start
    while place < size and uri[place] not in "@/":
        place += 1
    place = place + 1 if place < size and uri[place] == "@" else start
    while True:
        if uri.startswith("[", place):  # an IPv6 host: whatever it holds, to its closing bracket
            place = uri.find("]", place + 1) + 1
            if place == 0:
                return ""
        else:
            while place < size and uri[place] not in ":/?,":
                place += 1
        end = uri[place] if place < size else ""
        if end == ":":  # the port
            place += 1
            while place < size and uri[place] not in "/?,":
                place += 1
            end = uri[place] if place < size else ""
        if end != ",":
            break
        place += 1
    if end == "/":  # the database
        place += 1
        while place < size and uri[place] != "?":
            place += 1
        end = uri[place] if place < size else ""
    return uri[place + 1 :] if end == "?" else ""


def _keyword_names(text: str) -> list[str]:
    """The names of a keyword/value address, lexed as libpq lexes it: a name ends at the equal sign or at a blank, its
    value is bare (to the next blank) or between single quotes, and a backslash takes the next character as it is."""
    names: list[str] = []
    size, place = len(text), 0
    while place < size:
        if text[place] in _BLANKS:
            place += 1
            continue
        begin = place
        while place < size and text[place] != "=" and text[place] not in _BLANKS:
            place += 1
        name = text[begin:place]
        while place < size and text[place] in _BLANKS:
            place += 1
        if place == size or text[place] != "=":
            break  # no equal sign: libpq refuses the address
        place += 1
        while place < size and text[place] in _BLANKS:
            place += 1
        names.append(name)
        if place < size and text[place] == "'":
            place += 1
            while place < size and text[place] != "'":
                place += 2 if text[place] == "\\" else 1
            place += 1
        else:
            while place < size and text[place] not in _BLANKS:
                place += 2 if text[place] == "\\" else 1
    return names


def _has_sslmode_alias(url: str) -> bool:
    """Whether ``url`` holds a parameter that libpq writes into ``sslmode`` (``_SSLMODE_ALIASES``). Found as libpq finds
    its parameters, and not by looking for the word: a URI's names are percent-decoded (``%73sl=true`` is ``ssl=true``),
    and the same text in a password, a database's name or a value is no parameter."""
    for start in _URI_STARTS:
        if url.startswith(start):
            names = [unquote(pair.partition("=")[0]) for pair in _uri_query(url, len(start)).split("&")]
            return any(name in _SSLMODE_ALIASES for name in names)
    return any(name in _SSLMODE_ALIASES for name in _keyword_names(url))


@functools.cache
def _variables() -> dict[str, str]:
    """``{option: its environment variable}`` for every option of this libpq that has one."""
    return {o.keyword.decode(): o.envvar.decode() for o in pq.Conninfo.get_defaults() if o.envvar}


@functools.cache
def _pins() -> dict[str, str]:
    """For every option this libpq has a variable for, the value it has when nothing sets it, in a form libpq takes for
    it: the path that is not there for the files of the home folder, the compiled default where there is one, else
    empty, which libpq reads as "not set" (but for the options of ``_UNSET``, which refuse it). The option list is
    libpq's own, so an option a later libpq adds is given too; ``service`` and ``require_auth`` have no such value."""
    pins: dict[str, str] = {}
    for option in pq.Conninfo.get_defaults():
        keyword = option.keyword.decode()
        if not option.envvar or keyword in _NO_VALUE:
            continue
        if keyword in _HOME_FILES:
            pins[keyword] = _NO_FILE
        elif keyword in _UNSET:
            pins[keyword] = _UNSET[keyword]
        else:
            pins[keyword] = option.compiled.decode() if option.compiled is not None else ""
    return pins


def _lost_path(schema: str) -> str:
    """What is said when a connection's search path is not the schema alone: it reaches PostgreSQL in the connection's
    start-up options alone."""
    return (
        f"the connections' search path is not the schema {schema} alone: something between the server and PostgreSQL "
        "(a pooler such as PgBouncer) drops the start-up options that carry it. Connect to PostgreSQL itself, or let "
        "the pooler pass them on"
    )


def _options(schema: str) -> str:
    """The start-up options of a connection: the search path (the schema alone) and the settings of ``_PINNED``."""
    pinned = [f"-c {name}={value.replace(' ', _BACKSLASH + ' ')}" for name, value in _PINNED.items()]
    return " ".join([f"-c search_path={schema}", *pinned])


def _encoding(conn: psycopg.Connection[Any]) -> str:
    """The database's encoding as PostgreSQL names it. (In SQL_ASCII psycopg gives text as bytes.)"""
    row = conn.execute("SELECT current_setting('server_encoding')").fetchone()
    found = row[0] if row is not None else ""
    return found.decode("ascii", "replace") if isinstance(found, bytes) else str(found)


def _not_utf8(encoding: str) -> str:
    """What is said when the database is not UTF8: the setting, and the encoding PostgreSQL names."""
    return (
        f"the database's encoding (server_encoding) is {encoding} and the server needs UTF8: the names, labels and "
        "texts it keeps come from any script, and a database in another encoding refuses the characters it lacks, so "
        "that what a person is called or what an administrator did could fail to be kept. Create the database with "
        "ENCODING 'UTF8'"
    )


def _unpinned(conn: psycopg.Connection[Any]) -> list[str]:
    """The settings of ``_PINNED`` that this connection does not have, by name."""
    rows = conn.execute("SELECT name, setting FROM pg_settings WHERE name = ANY(%s)", (list(_PINNED),)).fetchall()
    found = dict(rows)
    return [name for name, wanted in _PINNED.items() if found.get(name) != wanted]


def _lost_pins(names: list[str]) -> str:
    """What is said when a connection does not have settings of ``_PINNED``: they reach PostgreSQL in the connection's
    start-up options alone."""
    wanted = ", ".join(f"{name} {_PINNED[name]}" for name in names)
    return (
        "the connections do not have the settings that the server sets for every one of them and rests on "
        f"({wanted}): something between the server and PostgreSQL (a pooler such as PgBouncer) drops or changes the "
        "start-up options that carry them. Connect to PostgreSQL itself, or let the pooler pass them on"
    )


def _parameters(url: str, schema: str, connect_timeout: int) -> dict[str, str]:
    """What psycopg connects with for ``url``, ``schema`` and ``connect_timeout``: the address as it is written, and
    every option libpq has a variable for that the address leaves out, given here as the value it has when nothing sets
    it (``_pins``), so that libpq asks no variable, no password file or client certificate of the home folder; the
    connection's start-up options (``_options``): its search path, which is the schema alone, and the settings it rests
    on (``_PINNED``), the client's encoding given as libpq's own option too; and what gives up a connection that went
    silent (``_BOUNDS``), whatever the address says of these. Raises ValueError, in words that quote nothing of the
    address, for a schema or a time that is not allowed, a control character, an address the driver cannot read, one
    with ``ssl`` or ``requiressl`` among its parameters (libpq writes them into sslmode), one that names no host (PGHOST
    would) or a service file (it would fill in the rest), a PGSERVICE in the environment (an empty service is an error
    to libpq, so it cannot be set aside for one connection) and a variable of ``require_auth`` (the same), for
    ``sslrootcert=system`` beside an sslmode other than ``verify-full`` (libpq refuses it, saying only "weak sslmode"),
    for a verified connection with no root certificate named (the home folder's is not read), and for ``verify-full``
    with a place that gives a ``hostaddr`` and no ``host`` (there is no name to check the certificate against). Two
    values that libpq derives from another option stay derived: sslmode from ``sslrootcert=system``, and the highest
    protocol version from the lowest one."""
    if _SCHEMA.fullmatch(schema) is None or schema in _SHARED or schema.startswith("pg_"):
        raise ValueError(
            "a schema's name is lowercase letters, digits and underscores, from a letter, up to 63 characters; "
            "public, information_schema and pg_* are not a server's own"
        )
    if connect_timeout not in _TIMEOUTS:
        raise ValueError("connect_timeout is 2 to 120 seconds")
    if any(char < " " or char == "\x7f" for char in url):  # libpq ends an address at a zero byte: what follows is lost
        raise ValueError(
            "the database address holds a control character (a zero byte, a line break...): it is refused before it "
            "is read"
        )
    if "PGSERVICE" in os.environ:  # even an empty one: libpq takes it for a service named "", which it cannot find
        raise ValueError(
            "PGSERVICE is set: a service file would fill in what the database address leaves out, and the address is "
            "all a server connects with. Unset PGSERVICE"
        )
    found: dict[str, Any] = {}
    kind = ""
    try:
        found = conninfo_to_dict(url)
    except Exception as exc:  # the words of some quote the address, the arguments of others hold it: only the type goes
        kind = type(exc).__name__
    if kind:
        raise ValueError(f"the database address cannot be read by PostgreSQL's driver ({kind})")
    if _has_sslmode_alias(url):  # libpq has read it into sslmode: what the address says of sslmode is not what is used
        raise ValueError(
            "the database address has ssl or requiressl among its parameters, which libpq writes into sslmode, the "
            "last parameter written winning, so that the sslmode that is used is not the one that is written: write "
            "sslmode itself, sslmode=verify-full&sslrootcert=system"
        )
    if not _hosts_named(found):
        raise ValueError(
            "the database address names no host, or leaves one of its hosts empty, and the server does not take one "
            "from PGHOST: write it in the address, postgresql://latet@db.acme.lan/latet (a socket's folder is "
            "?host=/var/run/postgresql)"
        )
    if "service" in found:
        raise ValueError(
            "the database address names a service file (service=): the host, the port and the user are written in "
            "the address itself"
        )
    required = _variables().get("require_auth", "")
    if required in os.environ and "require_auth" not in found:  # no value of its own sets it aside (see _NO_VALUE)
        raise ValueError(
            f"{required} is set: it would decide which authentication methods the server accepts from PostgreSQL, and "
            f"the address is all a server connects with. Unset {required}, or write require_auth in the address"
        )
    parameters = {
        **_pins(), **found, **_BOUNDS, "options": _options(schema), "connect_timeout": str(connect_timeout),
        "client_encoding": _PINNED["client_encoding"],
    }  # fmt: skip
    for keyword in _HOME_FILES:  # an empty name of a file is the one in the home folder to libpq: here it is none
        if keyword in parameters and not parameters[keyword]:
            parameters[keyword] = _NO_FILE
    # Two values that libpq derives from another option, when the address leaves them out, stay derived: the pins above
    # would otherwise give them their own. sslrootcert=system with no sslmode is verify-full (libpq refuses a weaker
    # mode with it, so the pin "prefer" would fail every connection), and a minimum protocol version above 3.0 lifts the
    # maximum, which is 3.0 otherwise (the pin 3.0 would be below it).
    if "sslmode" not in found and parameters.get("sslrootcert") == "system":
        parameters["sslmode"] = "verify-full"
    minimum = found.get("min_protocol_version")
    if minimum and minimum != "3.0" and "max_protocol_version" not in found:
        parameters["max_protocol_version"] = minimum
    if parameters.get("sslrootcert") == "system" and parameters.get("sslmode") != "verify-full":
        raise ValueError(
            "the database address names sslrootcert=system with an sslmode other than verify-full: libpq takes the "
            "system's certificate authorities with verify-full alone, which checks the host's name too: write "
            "sslmode=verify-full, or leave sslmode out"
        )
    if parameters.get("sslmode") in ("verify-ca", "verify-full") and parameters.get("sslrootcert") == _NO_FILE:
        raise ValueError(
            "the database address asks for a verified connection (sslmode) and names no sslrootcert, and the root "
            "certificates of this computer's home folder are not read: write sslrootcert=system for the system's "
            "certificate authorities, or the path of a file"
        )
    if parameters.get("sslmode") == "verify-full" and any(address and not host for host, address in _places(found)):
        raise ValueError(
            "the database address asks for sslmode=verify-full and gives a place by its hostaddr alone: the "
            "certificate is checked against the name of a host, so write the host's name beside the address (host)"
        )
    return parameters


class _Connection(psycopg.Connection[Any]):
    """A connection of the pool or of the migrations. When one cannot be made, what is raised says the type of the
    driver's error and nothing else: psycopg's pool writes the words of that error in its log, and a connection that
    fails to open is where a driver quotes the address, the user and, now and then, the password. Each of its waits for
    what PostgreSQL answers lasts ``ANSWER_SECONDS`` at most, but on the migrations' connection (``unbounded``)."""

    unbounded = False
    _deadline: float | None = None
    _deadline_clock: Callable[[], float] = staticmethod(time.monotonic)

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: Any) -> Self:
        kind = ""
        try:
            return super().connect(conninfo, **kwargs)
        except Exception as exc:  # nothing is raised while it is handled: it would stay as the context of the new one
            kind = type(exc).__name__
        raise DatabaseError(f"the connection to PostgreSQL failed ({kind})")

    def wait[RV](self, gen: PQGen[RV], interval: float = 0.1, timeout: float | None = None) -> RV:
        """psycopg's wait (every statement, commit and rollback goes through it), bounded when its caller gives no time
        of its own: past ``ANSWER_SECONDS`` the connection is closed, since what it asked may still be under way, and
        the pool makes another. Raises OperationalError then, in words that hold nothing of the address."""
        bound = timeout if timeout is not None or self.unbounded else ANSWER_SECONDS
        if self._deadline is not None:
            remaining = self._deadline - self._deadline_clock()
            if remaining <= 0:
                self.pgconn.finish()
                raise psycopg.OperationalError(
                    "the PostgreSQL page-work deadline was reached: the connection is closed"
                )
            bound = remaining if bound is None else min(bound, remaining)
        try:
            return super().wait(gen, interval, bound)
        except _WAITED_TOO_LONG:
            if timeout is not None and self._deadline is None:  # the caller's own time: as psycopg says it
                raise
            self.pgconn.finish()
        raise psycopg.OperationalError(f"PostgreSQL did not answer within {bound:g} seconds: the connection is closed")


class Database:
    """A pool of connections to one schema of PostgreSQL. ``connect_timeout`` bounds each attempt at a connection, the
    pool's and the migrations' alike, and the pool gives a request ten seconds to get one."""

    def __init__(
        self,
        url: str,
        *,
        schema: str = "latet",
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout: int = CONNECT_TIMEOUT,
    ) -> None:
        self.schema = schema
        self._parameters = _parameters(url, schema, connect_timeout)
        self.pool = ConnectionPool(
            "", connection_class=_Connection, kwargs=self._parameters, min_size=min_size, max_size=max_size,
            timeout=10.0, open=True, name=f"latet-{schema}",
        )  # fmt: skip
        # The applications calls that keep their values together at once (``vault.PgVaultStore.together``, Ruling
        # 10b-R2): each holds a connection until it ends, so a few of the pool's, never most of it (3 of 10); a call
        # that finds none free keeps each of its values at once, as before, and every other identity's work finds the
        # pool.
        self.together_at_most = max(1, max_size * 3 // 10)
        self.together = threading.BoundedSemaphore(self.together_at_most)

    @contextmanager
    def connection(
        self, *, deadline: float | None = None, clock: Callable[[], float] = time.monotonic
    ) -> Iterator[psycopg.Connection[Any]]:
        """A connection, committed on success and rolled back on failure.

        A page's optional absolute deadline bounds pool and driver waits, including final commit/rollback. Restore
        its metadata after transaction exit but before returning the connection for another borrower. A timed-out
        driver wait closes the uncertain connection. Ordinary callers and migrations retain their existing bounds.
        """
        if deadline is None:
            with self.pool.connection() as conn:
                yield conn
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("the PostgreSQL page-work deadline was reached")
        conn = self.pool.getconn(timeout=min(10.0, remaining))
        previous: tuple[float | None, Callable[[], float]] | None = None
        try:
            previous = conn._deadline, conn._deadline_clock
            conn._deadline, conn._deadline_clock = deadline, clock
            if deadline <= clock():
                conn.close()
                raise TimeoutError("the PostgreSQL page-work deadline was reached")
            with conn:
                yield conn
        finally:
            try:
                if previous is not None:
                    conn._deadline, conn._deadline_clock = previous
            finally:
                self.pool.putconn(conn)

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[Any]]:
        """A connection whose statements commit together, or not at all."""
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1")
        except (psycopg.Error, OSError):
            return False
        return True

    def close(self) -> None:
        self.pool.close()


def _lock_key(schema: str) -> int:
    """The advisory lock of a schema's migrations."""
    return int.from_bytes(hashlib.sha256(f"latet-migrate:{schema}".encode()).digest()[:8], "big", signed=True)


def _migrations(packages: Sequence[str]) -> list[tuple[str, str]]:
    """``(name, sql)`` of each NNN_name.sql of ``packages``: the packages in order, each one's files in order."""
    found: list[tuple[str, str]] = []
    for package in packages:
        files = sorted(
            (f for f in importlib.resources.files(package).iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
        )
        found += [(f"{package}:{f.name.removesuffix('.sql')}", f.read_text("utf-8")) for f in files]
    return found


def _search_path(conn: psycopg.Connection[Any]) -> list[str]:
    """The search path of a connection as PostgreSQL has it: the schemas of it that exist and that the role may use, in
    order, without the system's own (pg_catalog, pg_temp). Empty when none does."""
    row = conn.execute("SELECT current_schemas(false)").fetchone()
    return [] if row is None else list(row[0])


def _waited_for_the_lock() -> str:
    """What is said when another instance held the lock of the migrations for all of ``MIGRATION_WAIT``."""
    return (
        f"the lock of the migrations was held by another instance for {MIGRATION_WAIT} seconds, the longest an "
        "instance waits for it: another instance is migrating still (each migration is a transaction of its own), or a "
        "connection holds the lock without migrating (pg_locks shows it): start again once it is free"
    )


def migrate(db: Database, *, packages: Sequence[str] = MIGRATIONS) -> list[str]:
    """Bring the schema to this Latet's version, one instance at a time: the names of the migrations applied now, each
    in a transaction of its own, with no statement bound. The lock of the migrations is waited for ``MIGRATION_WAIT``
    at most. The schema is made only when it is not there, so that a role without CREATE on the database migrates a
    schema made for it. Raises DatabaseError, naming the step and the type of the driver's error, when PostgreSQL cannot
    be reached or refuses a statement, when the lock was not free in time, or when the connection's search path is not
    the schema alone (a pooler that drops the start-up options would have the tables made in another one), or does not
    have the settings the server rests on (``_PINNED``), or when the database's encoding is not UTF8."""
    files = _migrations(packages)
    applied: list[str] = []
    step, kind, waited = "the connection's settings could not be read", "", False
    try:
        with _Connection.connect("", autocommit=True, **db._parameters) as conn:
            conn.unbounded = True  # no ANSWER_SECONDS: see below
            unpinned = _unpinned(conn)  # the session as it came, before this connection changes some of it
            step = "the lock of the migrations could not be taken"
            # A migration takes the time it takes; the wait for another instance's has a bound of its own.
            conn.execute(
                "SELECT set_config('statement_timeout', '0', false), set_config('lock_timeout', %s, false)",
                (str(MIGRATION_WAIT * 1000),),
            )
            try:
                conn.execute("SELECT pg_advisory_lock(%s)", (_lock_key(db.schema),))
            except psycopg.errors.LockNotAvailable:
                waited = True
            if waited:
                raise DatabaseError(_waited_for_the_lock())
            try:
                step = "the schema could not be made ready for the migrations"
                conn.execute("SELECT set_config('lock_timeout', '0', false)")
                encoding = _encoding(conn)  # before anything is made: a database that is not UTF8 gets nothing
                if encoding != "UTF8":
                    raise DatabaseError(_not_utf8(encoding))
                there = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (db.schema,)).fetchone()
                if there is None:  # CREATE SCHEMA, even with IF NOT EXISTS, needs CREATE on the database
                    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(db.schema)))
                if _search_path(conn) != [db.schema]:  # now that it exists, it is the whole of the search path
                    raise DatabaseError(_lost_path(db.schema))
                if unpinned:
                    raise DatabaseError(_lost_pins(unpinned))
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(name TEXT PRIMARY KEY, applied DOUBLE PRECISION NOT NULL)"
                )
                done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
                for name, text in files:
                    if name in done:
                        continue
                    step = f"the migration {name} failed"
                    with conn.transaction():
                        conn.execute(text.encode())  # no parameters: the statements of the file go as they are
                        conn.execute(
                            "INSERT INTO schema_migrations (name, applied) VALUES (%s, %s)", (name, time.time())
                        )
                    applied.append(name)
            finally:
                with suppress(psycopg.Error):  # closing the connection releases the lock all the same
                    conn.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(db.schema),))
    except psycopg.Error as exc:
        kind = type(exc).__name__
    if kind:
        raise DatabaseError(f"{step} ({kind})")
    return applied


def check_schema(db: Database, *, packages: Sequence[str] = MIGRATIONS) -> None:
    """Raise SchemaMismatch when the schema is older or newer than this Latet's: an instance never runs on another.
    Raises DatabaseError, with the type of the driver's error, when PostgreSQL cannot be asked, or when the connection's
    search path is more than the schema (a pooler that drops the start-up options: a table of another schema would be
    taken for ours), or it does not have the settings the server rests on (``_PINNED``), or the database's encoding is
    not UTF8. A schema that is not there yet is an older one."""
    wanted = [name for name, _ in _migrations(packages)]
    have: set[str] = set()
    path: list[str] = []
    encoding, unpinned = "", []
    kind = ""
    try:
        with db.connection() as conn:
            encoding, path, unpinned = _encoding(conn), _search_path(conn), _unpinned(conn)
            if path in ([], [db.schema]):  # the names are read in the server's schema, or in none
                have = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    except psycopg.errors.UndefinedTable:
        have = set()
    except psycopg.Error as exc:
        kind = type(exc).__name__
    if kind:
        raise DatabaseError(f"the schema's migrations could not be read ({kind})")
    if encoding != "UTF8":
        raise DatabaseError(_not_utf8(encoding))
    if path not in ([], [db.schema]):
        raise DatabaseError(_lost_path(db.schema))
    if unpinned:
        raise DatabaseError(_lost_pins(unpinned))
    newer = sorted(have - set(wanted))
    if newer:
        raise SchemaMismatch(
            f"the database's schema {db.schema} is newer than this Latet ({', '.join(newer)}): upgrade Latet here"
        )
    missing = [name for name in wanted if name not in have]
    if missing:
        raise SchemaMismatch(
            f"the database's schema {db.schema} is older than this Latet ({len(missing)} to apply): run "
            "`latet server migrate`"
        )
