"""A Latet server's start-up settings: the LATET_SERVER_* variables (and LATET_MASTER_KEY_FILE) over a read-only
server.yaml. Nothing that changes while the server runs is here: the shared settings, the identities, their keys and
the license live in PostgreSQL."""

from __future__ import annotations

import ipaddress
import os
import re
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal
from urllib.parse import SplitResult, parse_qs, quote, unquote, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from latet.config import _yaml_error_at, load_yaml
from latet.fileio import FileReadError, read_regular_bytes
from latet.secret_text import KEY_BYTES, KEY_LONGEST, key_problem

SERVER_API = "/_latet/server/v1"  # the machines' calls, under the server's address
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_VARIABLES = {
    "url": "LATET_SERVER_URL",
    "database_url": "LATET_SERVER_DATABASE_URL",
    "valkey_url": "LATET_SERVER_VALKEY_URL",
    "master_key_file": "LATET_MASTER_KEY_FILE",
    "host": "LATET_SERVER_HOST",
    "port": "LATET_SERVER_PORT",
    "certificate": "LATET_SERVER_CERTIFICATE",
    "key": "LATET_SERVER_KEY",
    "log_format": "LATET_SERVER_LOG_FORMAT",
    "log_level": "LATET_SERVER_LOG_LEVEL",
    "metrics_token_file": "LATET_SERVER_METRICS_TOKEN_FILE",
    "reviewer_key_file": "LATET_SERVER_REVIEWER_KEY_FILE",
    "metrics_host": "LATET_SERVER_METRICS_HOST",
    "metrics_port": "LATET_SERVER_METRICS_PORT",
    "database_password_file": "LATET_SERVER_DATABASE_PASSWORD_FILE",
    "database_connect_timeout": "LATET_SERVER_DATABASE_CONNECT_TIMEOUT",
    "database_private_network": "LATET_SERVER_DATABASE_PRIVATE_NETWORK",
    "valkey_password_file": "LATET_SERVER_VALKEY_PASSWORD_FILE",
    "valkey_unauthenticated": "LATET_SERVER_VALKEY_UNAUTHENTICATED",
    "valkey_private_network": "LATET_SERVER_VALKEY_PRIVATE_NETWORK",
    "max_request_mb": "LATET_SERVER_MAX_REQUEST_MB",
    "requests_in_flight": "LATET_SERVER_REQUESTS_IN_FLIGHT",
    "vault_limit": "LATET_SERVER_VAULT_LIMIT",
    "history_limit_mb": "LATET_SERVER_HISTORY_LIMIT_MB",
    "admin_token_days": "LATET_SERVER_ADMIN_TOKEN_DAYS",
    "forwarded_allow_ips": "LATET_SERVER_FORWARDED_ALLOW_IPS",
    "valkey_connections": "LATET_SERVER_VALKEY_CONNECTIONS",
    "audit_retention_days": "LATET_SERVER_AUDIT_RETENTION_DAYS",
    "ocr_readers": "LATET_SERVER_OCR_READERS",
    "ocr_read_seconds": "LATET_SERVER_OCR_READ_SECONDS",
    "ocr_wait_seconds": "LATET_SERVER_OCR_WAIT_SECONDS",
}
# How many days the administration log keeps an entry when the settings say nothing (audit_retention_days): what the
# beat's hourly prune drops is older (``latet.server.app._audit``).
AUDIT_DAYS = 365
# How many connections to Valkey an instance keeps at most, for every call it makes but listening (the setting
# valkey_connections): past them a call waits for one (``latet.server.bus.POOL_WAIT``).
VALKEY_CONNECTIONS = 100
# What the bus gives its connections to Valkey itself, which redis-py would let an address's parameters win over: the
# times a connection and an answer have, the keepalives, the health check, the pool's size and its wait, and the name of
# the listening connection.
_VALKEY_OWN = frozenset(
    {"socket_timeout", "socket_connect_timeout", "socket_keepalive", "health_check_interval", "max_connections",
     "timeout", "client_name"}
)  # fmt: skip
# The narrowest network of each family that the proxy's addresses may be: two networks of a length of 1 are every IPv4
# address. And IPv4 addresses as a listener on both families sees them (::ffff:a.b.c.d) count as IPv4 addresses.
_PROXY_LENGTHS = {4: 8, 6: 32}
_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")
# The passwords that may come from a file (a Docker secret, say), put into the address they belong to.
_PASSWORD_FILES = (("database_url", "database_password_file"), ("valkey_url", "valkey_password_file"))
# How redis-py reads the scheme of a Valkey address: valkey:// is redis://, valkeys:// is rediss:// (TLS), and the three
# it has are themselves. One table for the settings, which judge an address and say which are TLS, and for the bus
# (``latet.server.bus.redis_url``), which connects with it: a scheme the settings call TLS is one the connection makes
# TLS, and one the settings accept is one redis-py reads.
VALKEY_SCHEMES = {"redis": "redis", "rediss": "rediss", "valkey": "redis", "valkeys": "rediss", "unix": "unix"}
# How an address starts, as its driver wants it written (Python reads a scheme in any case, libpq and redis-py do
# not), and what is said of one that is of another kind.
_POSTGRES = ("postgresql://", "postgres://")
_VALKEY = tuple(f"{scheme}://" for scheme in VALKEY_SCHEMES)
_NOT_POSTGRES = (
    "a PostgreSQL address, such as postgresql://latet:password@db.acme.lan:5432/latet?sslmode=verify-full"
    "&sslrootcert=system"
)
_NOT_VALKEY = "a Valkey address, such as rediss://:password@cache.acme.lan:6379/0"
# A % that starts no escape, and the escape of the zero byte: libpq refuses both, and says so with what holds them.
_NO_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})|%00")
_BRACKETED = re.compile(r"\[([0-9A-Fa-f:.]+)\](:[0-9]*)?")  # a host in brackets, and its port
_ZONED = re.compile(r"\[([0-9A-Fa-f:.]+)%25[^\]]*\](:[0-9]*)?")  # an IPv6 host in brackets with a zone, and its port
_PARAMETER = re.compile(r"[a-z0-9_]+=[^=]*")  # one parameter of a PostgreSQL address, as libpq takes it
# The names that libpq writes into sslmode, the last parameter written winning: requiressl (1 is require, any other
# value prefer) and, in a URI, ssl=true (require). The sslmode of an address that holds one is not the one written.
_SSLMODE_ALIASES = ("ssl", "requiressl")


def _scheme(url: str) -> str:
    """The scheme of ``url``, or none for an address that cannot be read. The standard library's own error is not
    passed on: its words quote the address, and a password with it."""
    try:
        return urlsplit(url).scheme
    except ValueError:
        return ""


def _split(url: str) -> tuple[SplitResult, int | None] | None:
    """``url`` in its parts, and its port; None for an address whose host or port cannot be read. The standard
    library's own error is not passed on, and nothing is raised while it is handled: its words quote what it could
    not read, a password or a part of one."""
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:
        return None


def _spaced(url: str) -> bool:
    """Whether an address holds a space or a control character: urlsplit takes some of them off before it reads, and
    a driver takes them for a part of what they stand in."""
    return any(char.isspace() or char < " " or char == "\x7f" for char in url)


def _not_utf8(url: str, *, escapes: bool) -> bool:
    """Whether an address is not UTF-8 text: as it is written (on Linux a variable set in another encoding reaches
    Python with a lone surrogate for each byte that is not UTF-8) or, with ``escapes``, as its % escapes are read
    (``%e9`` by itself stands for no letter). Python's own error is not passed on: it holds the address."""
    try:
        (unquote(url, errors="strict") if escapes else url).encode()
    except UnicodeError:
        return True
    return False


def _in_brackets(host: str) -> bool:
    """Whether ``host``, which holds a bracket, is an IPv6 address between two of them, with at most its port after."""
    found = _BRACKETED.fullmatch(host)
    if found is None:
        return False
    try:
        ipaddress.IPv6Address(found[1])
    except ValueError:
        return False
    return True


def _zoned(host: str) -> bool:
    """Whether ``host`` is an IPv6 address between brackets with a zone after it (``%25`` and an interface), and at
    most its port after them."""
    found = _ZONED.fullmatch(host)
    if found is None:
        return False
    try:
        ipaddress.IPv6Address(found[1])
    except ValueError:
        return False
    return True


def _too_wide(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Whether ``network`` holds more addresses than a proxy's own may: an IPv4 one shorter than /8, an IPv6 one shorter
    than /32, and the IPv4 addresses one holds as IPv6 ones (::ffff:a.b.c.d) judged as an IPv4 network."""
    if network.prefixlen < _PROXY_LENGTHS[network.version]:
        return True
    if isinstance(network, ipaddress.IPv6Network) and network.overlaps(_MAPPED):
        return network.prefixlen - _MAPPED.prefixlen < _PROXY_LENGTHS[4]  # all of them when it holds the whole block
    return False


def _unreadable(name: str, url: str, *, libpq: bool, password_later: bool = False) -> str | None:
    """Why its driver would not read ``url`` as Latet does, in a sentence that names the setting and holds nothing of
    the address; None for an address both read alike. It is said here because a driver says it later in words of its
    own, which quote the address and its password (libpq: ``invalid percent-encoded token: "<the password>"``;
    redis-py: ``Port could not be cast to integer value as '<half of it>'``), or with an error that holds them (psycopg,
    for an escape that is no UTF-8 text: ``UnicodeDecodeError('utf-8', b'<the password>', ...)``), or reads another
    host out of the address and goes there with what it took for the password. What is left is one way to write an
    address: its scheme in small letters, then at most one @, with the user and the password before it and the host
    after; whatever else a password holds of ``@ / ? # [ ]`` is written as its % escape. ``libpq``: PostgreSQL's
    reader, which differs from urlsplit and redis-py in more places. ``password_later``: the address is judged
    before a file's password is put into it, so a user's name may still stand without one."""
    if _spaced(url):
        return f"{name} holds a space or a control character: in a password a space is written %20"
    if _not_utf8(url, escapes=False):
        return f"{name} is not UTF-8 text: what sets it is written in another encoding"
    starts, another = (_POSTGRES, _NOT_POSTGRES) if libpq else (_VALKEY, _NOT_VALKEY)
    start = next((start for start in starts if url.startswith(start)), None)
    if start is None and url.partition(":")[0].lower() + "://" not in starts:
        return another
    if start is None:  # another case, or one slash: the driver takes the whole for another kind of text, and quotes it
        listed = ", ".join(starts[:-1]) + " or " + starts[-1]
        return f"{name} starts with {listed}, written so: small letters, a colon and two slashes"
    if _NO_ESCAPE.search(url):
        return f"{name} holds a % that starts no escape, or %00: in a password % is written %25"
    if _not_utf8(url, escapes=True):
        return f"{name} holds a % escape that is not UTF-8 text: in a password % is written %25"
    netloc = re.split("[/?#]", url[len(start) :], maxsplit=1)[0]  # the user, the password, the host and the port
    rest = url[len(start) + len(netloc) :]
    if "@" in rest:  # the address ended before that @, at a / ? or # of the password
        return f"{name} holds @ after its host: in a password / ? # and @ are written %2F %3F %23 and %40"
    user, _, host = netloc.rpartition("@")
    if "@" in user:  # libpq ends the user and the password at the first, urlsplit and redis-py at the last
        return f"{name} holds @ more than once: in a user name or a password @ is written %40"
    if "[" in user or "]" in user:  # one of them opens or closes the host's for one reader and not for the other
        return f"{name} holds [ or ] before its host: in a user name or a password they are written %5B and %5D"
    if ("[" in host or "]" in host) and _zoned(host):  # an IPv6 address all the same: said for what it is
        how = "; PostgreSQL takes one as a parameter, ?host=fe80::1%25eth0" if libpq else ""
        return f"{name} has an IPv6 host with a zone (%25 and an interface after the address), not taken here{how}"
    if ("[" in host or "]" in host) and not _in_brackets(host):
        return f"{name} has a host in brackets that is not an IPv6 address between them, with at most its port after"
    if "%" in host:  # which a driver reads as the character it stands for, and urlsplit as it is written
        socket = "; a socket's folder is given as ?host=/var/run/postgresql" if libpq else ""
        return f"{name} has a % in its host: a host is written as it is{socket}"
    if not _scheme(url):  # the standard library refuses it for another reason, which it says with the address
        return another
    read = _split(url)
    if read is None:
        hosts = "; several hosts with ports of their own are given as ?host=a,b&port=1,2" if libpq else ""
        return f"{name} has a port that cannot be read: a number after the host{hosts}"
    if libpq:
        return _otherwise_to_libpq(name, rest)
    # Where Valkey is decides whether it is asked a password: no parameter says it again, whatever redis-py makes of
    # one (it lets the address's own host win today, and refuses a host beside a socket).
    given = parse_qs(read[0].query, keep_blank_values=True).keys()
    if {"host", "port", "path", "connection_class"} & given:
        return f"{name} names a host, a port or a socket among its parameters: they are written in the address itself"
    if _VALKEY_OWN & given:  # the bus sets them over the address's all the same: whoever wrote one is told
        return (
            f"{name} sets among its parameters what the server sets of its connections itself (the times a connection "
            "and an answer have, keepalives, the health check, the connections kept and the wait for one, the "
            "listening connection's name): take them out (valkey_connections is the number kept)"
        )
    named, _, password = user.partition(":")
    if named and not password and not password_later:  # redis://password@host: the password is the user's name
        return f"{name} has a user's name and no password: a password alone comes after a colon, redis://:password@host"
    return None


def _otherwise_to_libpq(name: str, rest: str) -> str | None:
    """What libpq alone reads otherwise, in what follows the host of an address the checks above let through: to it a
    # is a character like another (the database's name and its parameters go on after one), and a parameter that is
    not name=value is refused in words that quote it whole (``?password:<the password>``)."""
    if "#" in rest:
        return f"{name} holds #: in a database's name or a parameter it is written %23"
    parameters = rest.partition("?")[2].split("&")
    if parameters[-1] == "":  # none at all, or an & at the end
        parameters.pop()
    if any(_PARAMETER.fullmatch(parameter) is None for parameter in parameters):
        return f"{name} has a parameter that is not name=value: each is written so, its name in small letters"
    if any(parameter.partition("=")[0] in _SSLMODE_ALIASES for parameter in parameters):
        return (
            f"{name} has ssl or requiressl among its parameters, which libpq writes into sslmode, the last parameter "
            "written winning, so that the sslmode that is used is not the one that is written: write sslmode itself"
        )
    return None


def _network(part: str, *, strict: bool) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """``part`` as an address or a network, None for what is neither; with ``strict``, None too for a network written
    with an address's bits in it (10.0.5.7/24)."""
    try:
        return ipaddress.ip_network(part, strict=strict)
    except ValueError:
        return None


def _here(host: str) -> bool:
    """Whether ``host`` is this machine: localhost, a loopback address or a socket's folder. The one test of "on this
    machine" for every address of a server: its own may be plain http there, and Valkey asks no password, and
    PostgreSQL and Valkey need no TLS."""
    return host.lower() in _LOOPBACK or host.startswith("/")


def _postgres_query(url: str) -> dict[str, str]:
    """The parameters of a PostgreSQL address, percent-decoded, each with its last value (as libpq has it)."""
    found: dict[str, str] = {}
    for pair in url.partition("?")[2].split("&"):
        name, _, value = pair.partition("=")
        if name:
            found[name] = unquote(value)
    return found


def _postgres_places(url: str) -> list[tuple[str, str]]:
    """``(host, hostaddr)`` of each place of a PostgreSQL address' list of hosts: the hosts of the address, which a
    ``host`` among the parameters replaces as libpq has it, and the addresses of ``hostaddr``, which libpq connects to
    in their place. ``url`` is an address that ``_unreadable`` let through: at most one @, no # and no % in a host,
    brackets only around an IPv6 host."""
    query = _postgres_query(url)
    start = next(start for start in _POSTGRES if url.startswith(start))
    netloc = re.split("[/?#]", url[len(start) :], maxsplit=1)[0].rpartition("@")[2]
    hosts: list[str] = []
    for member in netloc.split(",") if netloc else []:
        if member.startswith("["):
            hosts.append(member[1:].partition("]")[0])
        else:  # host:port, or a bare host (a bare IPv6 address, with its colons, is kept whole)
            hosts.append(member.partition(":")[0] if member.count(":") == 1 else member)
    if "host" in query:
        hosts = query["host"].split(",")
    addresses = query["hostaddr"].split(",") if "hostaddr" in query else []
    return list(zip_longest(hosts, addresses, fillvalue=""))


def _postgres_goes_to(url: str) -> list[str]:
    """Where a PostgreSQL address connects, place by place of its list of hosts: the address it names (``hostaddr``)
    or else the host. An empty place is the default host, which is this machine's."""
    return [address or host for host, address in _postgres_places(url)]


def _postgres_sslmode(url: str) -> str:
    """The sslmode libpq will use for a PostgreSQL address that ``_unreadable`` let through (so with no ``ssl`` or
    ``requiressl`` among its parameters, which libpq would write into it): the last one written, else ``verify-full``
    when the root certificate is ``system``, else ``prefer``, which is libpq's own."""
    query = _postgres_query(url)
    if "sslmode" in query:
        return query["sslmode"]
    return "verify-full" if query.get("sslrootcert") == "system" else "prefer"


def _valkey_here(parts: SplitResult) -> bool:
    """Whether a Valkey address is this machine's: a socket, or a host that is."""
    return VALKEY_SCHEMES.get(parts.scheme) == "unix" or _here(parts.hostname or "")


def _valkey_unverified(query: str) -> bool:
    """Whether the parameters of a rediss:// address turn the certificate check off, as redis-py reads them: the first
    value of each, a blank one left out, ``ssl_cert_reqs`` anything but ``required`` (``none`` and ``optional`` check
    nothing a client needs, and any other word is an error at the first connection), ``ssl_check_hostname`` one of
    its words for false."""
    values = parse_qs(query)
    required = values.get("ssl_cert_reqs", ["required"])[0]
    hostname = values.get("ssl_check_hostname", ["true"])[0]
    return required != "required" or hostname.upper() in ("0", "F", "FALSE", "N", "NO")


class ServerSettings(BaseModel):
    # No value in an error's words: pydantic would quote what it refused, an address with its password for one. Its
    # errors() and json() still hold what was given: load_server_settings raises its words alone.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(description="The address people use, such as https://latet.acme.lan (http on this machine only).")
    database_url: str = Field(
        repr=False,
        description="PostgreSQL: postgresql://user:password@host:5432/latet, with ?sslmode=verify-full&sslrootcert=... "
        "off this machine.",
    )
    valkey_url: str = Field(
        repr=False, description="Valkey or Redis: redis://host:6379/0 on this machine, rediss://:password@host off it."
    )
    master_key_file: Path | None = Field(default=None, description="The master key (LATET_MASTER_KEY gives it too).")
    host: str = Field(default="0.0.0.0", description="Where an instance listens.")  # noqa: S104 - a server's
    port: int = Field(default=5283, ge=1, le=65535, description="The port an instance listens on.")
    certificate: Path | None = Field(default=None, description="TLS at the instance (else at the load balancer).")
    key: Path | None = Field(
        default=None, description="The private key of the instance's certificate, in a private file."
    )
    log_format: Literal["text", "json"] = Field(
        default="text", description="How the log's lines are written on the standard output: text or json."
    )
    log_level: str = Field(
        default="info", description="What Latet's own lines say from: one of debug, info, warning, error."
    )
    metrics_token_file: Path | None = Field(
        default=None, description="A file holding the token Prometheus asks the metrics with: no file, no metrics."
    )
    reviewer_key_file: Path | None = Field(
        default=None,
        description="A file holding the local reviewer's own API key, never another secret, read once at start. "
        "Every identity's reviewer on this server sends it to the shared reviewer's address set by the "
        "administrators, over https or on this machine. The server reads no variable, keyring or saved key for it.",
    )
    metrics_host: str = Field(
        default="127.0.0.1", description="Where an instance answers Prometheus: an address only Prometheus reaches."
    )
    metrics_port: int = Field(
        default=7283,
        ge=1,
        le=65535,
        description="The port where an instance answers Prometheus, with metrics_token_file: never the instance's "
        "own port (port).",
    )
    database_password_file: Path | None = Field(default=None, description="A file holding PostgreSQL's password.")
    database_connect_timeout: int = Field(
        default=5, ge=2, le=120, description="Seconds PostgreSQL has to answer a new connection before it is given up."
    )
    database_private_network: bool = Field(
        default=False, description="Reach PostgreSQL over a network that only the instances share, with no checked TLS."
    )
    valkey_password_file: Path | None = Field(default=None, description="A file holding Valkey's password.")
    valkey_unauthenticated: bool = Field(
        default=False, description="Reach a Valkey that asks no password: only one nothing else can reach."
    )
    valkey_private_network: bool = Field(
        default=False, description="Reach Valkey over a network that only the instances share, with no checked TLS."
    )
    max_request_mb: int = Field(default=32, ge=1, le=1024, description="The largest request body, in MiB.")
    requests_in_flight: int = Field(
        default=16,
        ge=1,
        le=1024,
        description="How many requests one identity may have in flight on an instance, counted separately for its "
        "destinations (HTTP or WebSocket turns), dashboard and bridge: past each limit, 429. Long polls and streams "
        "have their own limits.",
    )
    vault_limit: int = Field(
        default=200_000, ge=1_000, le=10_000_000, description="How many values one identity's vault may hold."
    )
    history_limit_mb: int = Field(
        default=1024,
        ge=1,
        le=1_048_576,
        description="How many MiB one identity's history may keep: past it, its oldest exchanges go.",
    )
    admin_token_days: int = Field(
        default=90, ge=1, le=3650, description="How many days a new administration token lasts (--never opts out)."
    )
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description="The proxy's addresses, whose X-Forwarded-For and X-Forwarded-Proto are believed (never *, nor a "
        "network wider than /8 for IPv4 or /32 for IPv6).",
    )
    valkey_connections: int = Field(
        default=VALKEY_CONNECTIONS,
        ge=4,
        le=10_000,
        description="How many connections to Valkey an instance keeps at most: past them a call waits for one, five "
        "seconds at most.",
    )
    audit_retention_days: int = Field(
        default=AUDIT_DAYS, ge=30, le=3650, description="How many days the administration log keeps an entry."
    )
    # The text recognition's bounds (latet.vision.pool's SPACE_READERS, SPACE_READ_SECONDS and SPACE_WAIT_SECONDS, with
    # the measures they come from: written here as numbers, since this module imports nothing of the text recognition,
    # and a test pins them equal).
    ocr_readers: int = Field(
        default=2,  # SPACE_READERS
        ge=1,
        le=16,
        description="How many text recognitions an instance keeps, each reading one image at a time, the processors "
        "this instance may use shared among them (never more readers than processors): each holds its models in "
        "memory (0.05 GB at rest; the first brings ONNX Runtime, 0.7 GB), and up to 0.9 GB more while it reads a large "
        "screenshot.",
    )
    ocr_read_seconds: float = Field(
        default=30.0,  # SPACE_READ_SECONDS
        ge=2,
        le=120,
        description="How long one image's text may take to read: past it the image is withheld, or the request that "
        "only carries it stopped.",
    )
    ocr_wait_seconds: float = Field(
        default=35.0,  # SPACE_WAIT_SECONDS
        ge=2,
        le=300,
        description="How long a read waits at most, for its identity's other read and then for a text recognition: "
        "past it, as past the read's time. Never shorter than ocr_read_seconds.",
    )

    @field_validator("url")
    @classmethod
    def _url(cls, url: str) -> str:
        read = _split(url.strip())
        if read is None:
            raise ValueError("an address such as https://latet.acme.lan")
        parts, port = read
        host = (parts.hostname or "").lower()
        if not host or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not _here(host)):
            raise ValueError("an https:// address, such as https://latet.acme.lan (http on this machine only)")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("the address alone: no path, user, query or fragment")
        host = f"[{host}]" if ":" in host else host
        default = 443 if parts.scheme == "https" else 80
        return f"{parts.scheme}://{host}" + (f":{port}" if port not in (None, default) else "")

    @field_validator("database_url")
    @classmethod
    def _database(cls, url: str) -> str:
        if problem := _unreadable("database_url", url, libpq=True):
            raise ValueError(problem)
        return url

    @field_validator("valkey_url")
    @classmethod
    def _valkey(cls, url: str) -> str:
        if problem := _unreadable("valkey_url", url, libpq=False):
            raise ValueError(problem)
        return url

    @field_validator("forwarded_allow_ips")
    @classmethod
    def _proxies(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts or "*" in parts:
            raise ValueError("the proxy's addresses, such as 10.0.5.2 (never *: anyone could then pose as the proxy)")
        for place, part in enumerate(parts, 1):  # an entry is named by its place, never quoted
            # Read as uvicorn reads it: a network written with an address's bits in it is no network to uvicorn, which
            # then believes no proxy at all. The network to write is said: an address and a length hold no secret.
            network, loose = _network(part, strict=True), _network(part, strict=False)
            if loose is None:
                raise ValueError(
                    f"forwarded_allow_ips: entry {place} is not an address or a network, such as 10.0.5.2 or "
                    "10.0.5.0/24"
                )
            if loose.prefixlen == 0:  # however it is written: the network of a length of 0 is every address
                raise ValueError(
                    f"forwarded_allow_ips: entry {place} is every address, which is * by another name: anyone could "
                    "pose as the proxy"
                )
            if _too_wide(loose):  # two networks of a length of 1 are every IPv4 address as well
                raise ValueError(
                    f"forwarded_allow_ips: entry {place} is a network wider than a proxy's own: an IPv4 network is /8 "
                    "or longer, an IPv6 one /32 or longer (an IPv4 address written as an IPv6 one counts as IPv4): "
                    "every client in it could pose as the proxy"
                )
            if network is None:
                raise ValueError(
                    f"forwarded_allow_ips: entry {place} is an address with a network's length: write the network, "
                    f"{loose}"
                )
        return ",".join(parts)

    @model_validator(mode="after")
    def _pair(self) -> ServerSettings:
        if (self.certificate is None) != (self.key is None):
            raise ValueError("certificate and key go together")
        return self

    @model_validator(mode="after")
    def _metrics_socket(self) -> ServerSettings:
        """The metrics have a socket of their own, which a load balancer in front of the instances' port never reaches:
        an instance tells it from its own by the port a request came to."""
        if self.metrics_port == self.port:
            raise ValueError(
                "metrics_port is the instance's own port (port): the metrics have a socket of their own, which no load "
                "balancer reaches; give metrics_port another port (7283 by default)"
            )
        return self

    @model_validator(mode="after")
    def _reading(self) -> ServerSettings:
        """A read waits at least as long as another reads, so that it has its turn after one other identity's read; the
        default waits 5 s more, for a read that runs past its budget before it is ended (a tenth of a second here)."""
        if self.ocr_wait_seconds < self.ocr_read_seconds:
            raise ValueError(
                "ocr_wait_seconds is at least ocr_read_seconds: a read waits at least as long as another reads, and a "
                "little longer covers one that runs past its budget before it is ended (the default waits 5 s more)"
            )
        return self

    @model_validator(mode="after")
    def _valkey_password(self) -> ServerSettings:
        """Valkey holds every conversation's state and the sign-ins: never reached without a password, but on this
        machine (the tests) or where the settings say so."""
        parts = urlsplit(self.valkey_url)
        if not parts.password and not _valkey_here(parts) and not self.valkey_unauthenticated:
            raise ValueError(
                "Valkey needs a password: rediss://:password@host (or valkey_password_file naming a file that holds "
                "it); LATET_SERVER_VALKEY_UNAUTHENTICATED=1 only for a Valkey nothing else can reach"
            )
        return self

    @model_validator(mode="after")
    def _database_tls(self) -> ServerSettings:
        """PostgreSQL holds every identity's space and the master key's check: off this machine it is reached over TLS
        that checks the server (the sslmode that libpq will use is ``verify-full`` or ``verify-ca``: written in the
        address, which names the certificate authorities itself, or made of ``sslrootcert=system``), or on a network
        that only the instances share, where the settings say so. ``verify-full`` checks the certificate against a
        host's name, so a place of the address that gives a ``hostaddr`` and no ``host`` is refused for it; and libpq
        takes ``sslrootcert=system`` with ``verify-full`` alone, so another sslmode beside it is refused wherever the
        address leads (libpq would say only "weak sslmode"). Said without a part of the address."""
        off_machine = any(place and not _here(place) for place in _postgres_goes_to(self.database_url))
        sslmode = _postgres_sslmode(self.database_url)
        if _postgres_query(self.database_url).get("sslrootcert") == "system" and sslmode != "verify-full":
            raise ValueError(
                "database_url names sslrootcert=system with an sslmode other than verify-full: libpq takes the "
                "system's certificate authorities with verify-full alone, which checks the host's name too: write "
                "sslmode=verify-full, or leave sslmode out"
            )
        if off_machine and sslmode not in ("verify-full", "verify-ca") and not self.database_private_network:
            raise ValueError(
                "database_url is off this machine and does not ask for a TLS connection that checks the server: end "
                "the address with ?sslmode=verify-full&sslrootcert=system (or the path of a certificate authority's "
                "file); LATET_SERVER_DATABASE_PRIVATE_NETWORK=1 only for a network that only the instances share"
            )
        places = _postgres_places(self.database_url)
        if sslmode == "verify-full" and any(address and not host for host, address in places):
            raise ValueError(
                "database_url asks for sslmode=verify-full and gives a place by its hostaddr alone: the certificate is "
                "checked against the name of a host, so write the host's name beside the address (host)"
            )
        return self

    @model_validator(mode="after")
    def _valkey_tls(self) -> ServerSettings:
        """Valkey holds every conversation's state and the sign-ins: off this machine it is reached over TLS that checks
        the server (``rediss://``, the certificate checks left on), or on a network that only the instances share,
        where the settings say so. Said without a part of the address."""
        parts = urlsplit(self.valkey_url)
        if _valkey_here(parts) or self.valkey_private_network:
            return self
        if VALKEY_SCHEMES.get(parts.scheme) != "rediss":  # the schemes redis-py makes TLS of: rediss:// and valkeys://
            raise ValueError(
                "valkey_url is off this machine and is not a TLS address: use rediss:// (or valkeys://); "
                "LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 only for a network that only the instances share"
            )
        if _valkey_unverified(parts.query):
            raise ValueError(
                "valkey_url turns the certificate check off (ssl_cert_reqs other than required, or ssl_check_hostname "
                "false): take the parameter out; LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 only for a network that only "
                "the instances share"
            )
        return self


def with_password(url: str, password: str) -> str:
    """``url`` with ``password`` as its password; an address that has one keeps its own. Raises ValueError for an
    address that cannot be read, in words that hold nothing of it."""
    read = _split(url)
    if read is None:
        raise ValueError("an address whose host or port cannot be read")
    parts = read[0]
    if parts.password or not password:
        return url
    where = parts.netloc.rpartition("@")[2]  # the host and the port as they are written, not as they are read
    netloc = f"{parts.username or ''}:{quote(password, safe='')}@{where}"
    scheme = url.partition(":")[0] if parts.scheme else ""  # as it is written: its case is the settings' to judge
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def _file_settings(path: Path) -> dict[Any, Any]:
    """What server.yaml holds. Raises ValueError for a file that is not text, not YAML or not settings, saying where
    and never what: PyYAML's words quote the line and a decoding error holds the whole file, and either may hold a
    password, so nothing is raised while one of them is handled."""
    text, problem = "", ""
    try:
        text = path.read_text("utf-8")
    except UnicodeDecodeError:
        problem = f"{path} is not UTF-8 text"
    loaded: Any = None
    if not problem:
        try:
            loaded = load_yaml(text) or {}
        except yaml.YAMLError as exc:
            problem = f"{path} is not valid YAML{_yaml_error_at(exc, text)}"
    if problem:
        raise ValueError(problem)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} holds no settings")
    return loaded


PASSWORD_FILE_BYTES = 8 * 1024


def _with_file_password(key: str, url: str, file_key: str, file: Path) -> str:
    """``url``, the address of the setting ``key``, with the password that the file of ``file_key`` holds. The address
    is judged first, as it is written: put together again around a password, one whose scheme lacks its two slashes
    would come out with them and no host, which a driver reads as this machine. Raises ValueError for such an
    address, for a file that cannot be read or holds nothing, and for a password given twice. The settings are
    named, not what they hold (a password given where its file's name is wanted is not shown), and nothing is raised
    while the error of the read is handled: it holds the file's name, and a decoding error the file's bytes."""
    if unread := _unreadable(key, url, libpq=key == "database_url", password_later=True):
        raise ValueError(unread if unread.startswith(key) else f"{key}: {unread}")
    password, problem = "", ""
    try:
        password = read_regular_bytes(file, limit=PASSWORD_FILE_BYTES, follow=True).decode("utf-8-sig").strip()
    except FileReadError as exc:
        problem = str(exc)
    except UnicodeDecodeError:
        problem = "cannot be read: it is not UTF-8 text"
    if problem:
        raise ValueError(f"{file_key} names a file that {problem}")
    if not password:  # not a start without the password that was meant
        raise ValueError(f"{file_key} names a file that holds no password")
    # Said of the file, here: escaped into the address, a zero byte would be refused there as an escape nobody wrote.
    if "\x00" in password:
        raise ValueError(
            f"{file_key} names a file that holds a zero byte: a file saved as UTF-16 has one beside each letter, and "
            "a password's file is UTF-8"
        )
    if any(char < " " or char == "\x7f" for char in password):  # a space is a password's own; a line break is not
        raise ValueError(
            f"{file_key} names a file that holds more than one line, or a control character: a password is one line "
            "of text"
        )
    read = _split(url)
    if read is None:  # none that was judged readable above: kept so that nothing here leans on that
        raise ValueError(f"{key}: an address whose host or port cannot be read")
    # libpq takes a password of the query over the address's own, an empty one too; redis-py the other way round.
    if read[0].password or "password" in parse_qs(read[0].query, keep_blank_values=True):
        raise ValueError(f"{key} holds a password and {file_key} names a file that holds one: give it once")
    return with_password(url, password)


SECRET_LONGEST = KEY_LONGEST
SECRET_BYTES = KEY_BYTES


def read_secret_file(path: Path, setting: str) -> str:
    """Read one bounded regular UTF-8 file, following secret-volume links; errors name only the setting."""
    content, problem = b"", ""
    try:
        content = read_regular_bytes(path, limit=SECRET_BYTES, follow=True)
    except FileReadError as exc:
        problem = str(exc)
    if problem:
        raise ValueError(f"{setting} names a file that {problem}")
    text = ""
    try:
        text = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        problem = "cannot be read: it is not UTF-8 text"
    if problem:
        raise ValueError(f"{setting} names a file that {problem}")
    invalid = key_problem(text)
    if invalid == "empty":
        raise ValueError(f"{setting} names a file that holds nothing")
    if invalid == "characters":
        raise ValueError(
            f"{setting} names a file that holds more than one line, a space, a control character or a character "
            "outside ASCII: a key is one line of printable ASCII"
        )
    if invalid == "long":
        raise ValueError(f"{setting} names a file that holds more than {SECRET_LONGEST} characters")
    return text


def load_server_settings(path: Path | None = None) -> ServerSettings:
    """``path`` (server.yaml; LATET_SERVER_CONFIG's file when None, if set), each LATET_SERVER_* variable over it; the
    passwords of ``database_password_file`` and ``valkey_password_file`` put into their addresses. Raises ValueError
    for settings that cannot be used: a plain one, which holds its words and nothing else (no value of a setting, no
    error to follow back to one, where pydantic's own keeps what it refused), and OSError for a server.yaml that
    cannot be opened."""
    if path is None and os.environ.get("LATET_SERVER_CONFIG"):
        path = Path(os.environ["LATET_SERVER_CONFIG"])
    data: dict[str, Any] = {}
    if path is not None:
        data.update(_file_settings(path))
    for key, variable in _VARIABLES.items():
        if value := os.environ.get(variable):
            data[key] = value
    for key, file_key in _PASSWORD_FILES:
        if data.get(file_key) and data.get(key):
            data[key] = _with_file_password(key, str(data[key]), file_key, Path(str(data[file_key])))
    problem = ""
    try:
        return ServerSettings.model_validate(data)
    except ValidationError as exc:
        problem = str(exc)
    raise ValueError(problem)
