"""The Enterprise settings: a section for each Enterprise feature that has settings (each later part adds its own: E3
its sign-in, E4 its reports and SIEM outputs, E5 its approvals, E6 its term sources). They are a Latet server's alone:
a row for each section in its PostgreSQL (latet.ee.server.DatabaseSettings), set through its administration API. A
secret is named here, never kept here: the server's secrets keep it (latet.ee.secrets)."""

from __future__ import annotations

import codecs
import ipaddress
import json
import logging
import re
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)

MAX_BYTES: Final = 1024 * 1024  # a section holds a few kilobytes: a larger one is not kept


class EnterpriseSettingsError(ValueError):
    """Enterprise settings that cannot be used: the message says where, and what is wrong, never what the value given
    holds."""


class UnknownSection(EnterpriseSettingsError):
    """A section this version of Latet does not have."""

    def __init__(self, name: str) -> None:
        super().__init__(f"there is no Enterprise setting named {name!r}")
        self.name = name


# The sso section (E3): single sign-on to a server's dashboard with the company's identity provider, and the Latet
# groups its ID tokens' groups map to. client_secret names a secret kept sealed with the server's master key
# (`latet server sign-in secret`), never holds it.
DEFAULT_SECRET = "sso-client-secret"  # noqa: S105 - the name of a secret, not one
# The secrets the sign-in may name begin so: the Sign-in page and `latet server sign-in secret` save and remove the one
# client_secret names, never another part's (a SIEM's token, E4).
SSO_SECRETS = "sso-"
# The secrets the SIEM outputs may name begin so: `latet server siem secret` and the SIEM outputs' page save and remove
# those alone, never the sign-in's (SSO_SECRETS), nor the sign-in another part's (R11).
SIEM_SECRETS = "siem-"
GOOGLE = "https://accounts.google.com"  # its ID tokens say the person's Workspace domain in hd
_SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


class SsoSettings(BaseModel):
    """Single sign-on with OpenID Connect (authorization code with PKCE).

    ``accounts``: who gets an account at a first sign-in: ``scim`` (only people SCIM provisioned, or that an
    administrator linked), ``group`` (members of ``access_group``) or ``open`` (anyone the provider signs in, written
    out). ``allowed_domains``: Google's Workspace domains, checked against its ID tokens' ``hd``. ``company_changes``:
    whether the dashboard's company-wide changes need a session opened with this sign-in (``sso``) or any
    administrator's session takes them (``any-session``); changed with `latet server` alone."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    issuer: str
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str | None = DEFAULT_SECRET
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"], max_length=32)
    user_name_claim: str = Field(default="preferred_username", max_length=256)
    groups_claim: str = Field(default="groups", max_length=256)
    accounts: Literal["scim", "group", "open"]
    access_group: str | None = Field(default=None, max_length=256)
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    groups: dict[str, str] = Field(default_factory=dict, max_length=500)  # a group of the ID token → a Latet group
    company_changes: Literal["sso", "any-session"] = "sso"

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        """Its twin is ``latet.ee.oidc.secure_address``: https, or http on this computer (Keycloak by hand)."""
        value = value.strip()
        try:
            parts = urlsplit(value)
            parts.port  # noqa: B018 - a port that is not a number raises here
        except ValueError:
            raise ValueError("the issuer is an https:// address") from None
        loopback = parts.hostname in _LOOPBACK_NAMES
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise ValueError("the issuer is an https:// address (http:// only on this computer, for a test)")
        if not parts.hostname or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("the issuer is an address with no query, no fragment and no user")
        return value

    @field_validator("client_secret")
    @classmethod
    def _secret_name(cls, value: str | None) -> str | None:
        if value is not None and (_SECRET_NAME.fullmatch(value) is None or not value.startswith(SSO_SECRETS)):
            raise ValueError(
                f"client_secret names a secret saved with `latet server sign-in secret`, beginning {SSO_SECRETS}: the "
                "secret itself never goes in the settings"
            )
        return value

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        if "openid" not in value or any(
            not scope or len(scope) > 128 or not scope.isascii() or not scope.isprintable() or " " in scope
            for scope in value
        ):
            raise ValueError("scopes are single words, openid among them")
        return value

    @field_validator("user_name_claim", "groups_claim")
    @classmethod
    def _claim(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("the name of a claim of the ID token")
        return value

    @field_validator("access_group")
    @classmethod
    def _access_group(cls, value: str | None) -> str | None:
        return None if value is None else _token_group(value)

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        kept: list[str] = []
        for domain in value:
            domain = domain.strip().lower()
            if not domain.isascii() or _DOMAIN.fullmatch(domain) is None:
                raise ValueError("an allowed domain is a domain name, such as acme.com")
            if domain not in kept:
                kept.append(domain)
        return kept

    @field_validator("groups")
    @classmethod
    def _groups(cls, value: dict[str, str]) -> dict[str, str]:
        """Each group of the ID token compared as ``latet.ee.people.group_key`` does, each Latet group's name kept as
        ``group_name`` keeps it (its twins in ee/people.py)."""
        from latet.ee.people import group_key, group_name  # psycopg: the server extra, as every section's reader

        mapped: dict[str, str] = {}
        seen: set[str] = set()
        for claimed, group in value.items():
            key = group_key(_token_group(claimed))
            if key in seen:
                raise ValueError("a group of the ID token is written twice (case and a leading / aside)")
            seen.add(key)
            mapped[claimed.strip()] = group_name(group)
        return mapped

    @model_validator(mode="after")
    def _who_gets_an_account(self) -> SsoSettings:
        if self.accounts == "group" and self.access_group is None:
            raise ValueError("accounts: group needs access_group, the group of the ID token whose members get one")
        if self.accounts == "open" and self.access_group is not None:
            raise ValueError("access_group is for accounts: group (or scim); accounts: open lets anyone in")
        google = self.issuer.rstrip("/") == GOOGLE
        if google and not self.allowed_domains:
            raise ValueError("Google's sign-in needs allowed_domains: the Workspace domains whose people get in")
        if not google and self.allowed_domains:
            raise ValueError("allowed_domains is Google's (its ID tokens' hd claim): use access_group elsewhere")
        return self


def _token_group(value: str) -> str:
    """A group of the ID token as the settings name it: by its name or its ID, one line with no control or
    bidirectional character (an administrator reads it in a terminal), 256 characters at most."""
    from latet.server.identities import clean

    stripped = value.strip()
    if not stripped.lstrip("/"):
        raise ValueError("a group of the ID token is written by its name or its ID")
    if len(stripped) > 256 or clean(stripped, 256) != stripped:
        raise ValueError(
            "a group of the ID token is one line of at most 256 characters, with no control or bidirectional character"
        )
    return stripped


class ReportSettings(BaseModel):
    """``reports``: how people appear in reports and in the events sent to the SIEM, and how long the log keeps them.
    Set with `latet server reports settings` alone (a privacy choice of the company's: R4)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    people: Literal["group-only", "pseudonymous", "named"] = Field(
        default="group-only",
        description="How a person appears in reports and in the events sent to the SIEM: `group-only` (never: their "
        "group only), `pseudonymous` (the same pseudonym in every report) or `named`. An API key always appears by "
        "its name. A change applies from then on. In several countries a tool that can follow each employee's work "
        "needs an agreement first: see Reports and SIEM in the documentation.",
    )
    retention_days: int = Field(default=365, ge=1, le=3650, description="Days the event log keeps its counts.")


# A header or a query parameter whose name says it carries a credential: its value must be `{secret}`. `key`, `pass`
# and `auth` (with `authn`, `authz`, `authentication`) count as a part of a name (X-Functions-Key,
# Ocp-Apim-Subscription-Key, X-Insert-Key, X-Pass, X-Auth, X-Authentication), never as letters inside a word
# (X-Monkey, X-Passthrough, X-Author); `credential`, `passwd` and `pwd` wherever they stand.
_CREDENTIAL = re.compile(
    r"authorization|api[-_]?key|token|secret|signature|password|passwd|pwd|credential|cookie|^sig$|^code$"
    r"|(?:^|[-_])(?:key|pass)(?:$|[-_])|(?:^|[-_])auth(?:entication|n|z)?(?:$|[-_])",
    re.I,
)
_LOOPBACK = frozenset(_LOOPBACK_NAMES)
# I2: a header Latet sets itself, or one a proxy, a CDN or a browser sets: never an output's (a forged forwarding
# header sent to a service of the company's network, a second Host, a length that splits the request, a method
# changed, an answer asked for compressed).
_RESERVED = re.compile(
    r"host|content-length|content-type|content-encoding|transfer-encoding|te|trailer|connection|keep-alive|upgrade"
    r"|expect|accept-encoding|proxy-.*|forwarded|via|x-forwarded|x-forwarded-.*|x-real-ip|x-client-ip|true-client-ip"
    r"|x-cluster-client-ip|client-ip|cf-connecting-ip|fastly-client-ip|x-remote-ip|x-remote-addr|x-originating-ip"
    r"|x-azure-clientip|x-host|x-envoy-.*|x-original-.*|x-rewrite-url|x-http-method-override|x-http-method"
    r"|x-method-override|cookie|origin|referer|sec-.*|user-agent|x-latet-.*",
    re.I,
)
# I3, R2-11 and fix round 1: what looks like a token, in a segment of the path or in the query (a name or a value),
# each read percent-decoded as the receiver reads it (``unquote``: an escape that is none kept as written, bytes that
# are no UTF-8 one U+FFFD each). Searched within the text, so that `;key=…` or `….json` hides none:
#   - 32 hexadecimal digits or more, or a GUID (Loggly's, Datadog's key, a Teams webhook's);
#   - 20 characters or more of letters, digits and - _ . = ~ +, mixing digits with lower- and uppercase letters, with
#     12 or more between two of the separators - _ . ~ (Sumo Logic's, a Slack-style webhook's, base64, URL-safe
#     base64), so that words joined by separators (`Latet-Server-Prod-01`, `LATET-Server-01.Prod.Acme.Example.com`)
#     are none (fix round 2);
#   - 20 letters and digits or more between those separators, mixing digits with letters of either case (a key in
#     lowercase), so that words joined by hyphens with a number (`latet-events-2026.10`) are none.
# But a Logic App's workflow id, under `/workflows/` on `*.logic.azure.com` (an id, whose credential is the query's
# `sig`), and on a Splunk output its `channel`, a GUID that groups its acknowledgements: named exactly so, as written
# (no other case, no escape), once.
_HEX = re.compile(r"[0-9A-Fa-f]{32,}")
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_RUN = re.compile(r"[A-Za-z0-9_.=~+-]{20,}")
_RUN_PIECE = 12  # the longest piece of a run between separators, at least, for the run to be a token
_SEPARATORS = re.compile(r"[-_.~]")
_PIECE = re.compile(r"[A-Za-z0-9=+]{20,}")
# A host as an address names it: letters, digits, dots and hyphens (an IPv6 address comes in brackets, with no zone).
_HOSTNAME = re.compile(r"[a-z0-9.-]+")
_HOST = "a host is a name of letters, digits, dots and hyphens, or an IPv6 address in brackets with no zone"
_THIS_COMPUTER = ipaddress.ip_network("127.0.0.0/8")
# A host of numbers alone (decimal, octal or hex, dotted or not), which inet_aton reads as an IPv4 address: E3's
# ``oidc._NUMBERS``, written again here so that the settings import nothing of the sign-in.
_NUMBERS = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*")
_URL_LONGEST = 2048
_HEADERS_MOST = 32
_HEADER_LONGEST = 4096
_UNREADABLE = (
    "its url cannot be read: a scheme, a host, a port from 0 to 65535 when one is written, and brackets around an IPv6 "
    "address alone"
)


def _tokenish(text: str) -> bool:
    """Whether ``text`` (decoded) holds something shaped as a token (``_HEX``, ``_GUID``, ``_RUN``, ``_PIECE``)."""
    if _HEX.search(text) or _GUID.search(text):
        return True
    for run in _RUN.findall(text):
        if (
            any(c.isdigit() for c in run)
            and any(c.islower() for c in run)
            and any(c.isupper() for c in run)
            and max(len(piece) for piece in _SEPARATORS.split(run)) >= _RUN_PIECE
        ):
            return True
    return any(any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece) for piece in _PIECE.findall(text))


def _token_in(kind: str, host: str, path: str, query: str = "") -> bool:
    """Whether the address's path or query holds a token written as it is, not as ``{secret}`` (I3, R2-11), for an
    output of ``kind``."""
    segments = unquote(path).split("/")
    logic_app = _host(host).endswith(".logic.azure.com")
    for index, segment in enumerate(segments):
        if logic_app and index > 0 and segments[index - 1] == "workflows":
            continue
        if _tokenish(segment):
            return True
    pairs = [pair.partition("=") for pair in query.split("&")]
    channels = sum(1 for written, _, _ in pairs if unquote(written).lower() == "channel")  # in any form
    for written, _, given in pairs:
        name, value = unquote(written), unquote(given)
        if kind == "splunk" and written == "channel" and channels == 1 and _GUID.fullmatch(value):
            continue  # Splunk's channel, as written: no other case, no escape, once
        if _tokenish(name) or _tokenish(value):
            return True
    return False


def _host(host: str) -> str:
    """A host as it names a place: its case and a trailing dot aside (R2-5: `localhost.` is this computer)."""
    return host.rstrip(".").lower()


def _visible(text: str) -> bool:
    """Visible ASCII, code points 32 to 126: what an HTTP header's value is sent in (I8)."""
    return all(32 <= ord(c) <= 126 for c in text)


def _ipv6(host: str) -> bool:
    """Whether ``host`` is an IPv6 address (ipaddress's words, which quote it, never said)."""
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


_UNREACHED = (
    "the server never reaches this address: link-local (the cloud's metadata address among them), reserved, "
    "unspecified or multicast, an IPv6 form that carries an IPv4 address, or this computer under another name than "
    "localhost, 127.0.0.1 or [::1] (an IPv4 address is written in its usual form, four numbers from 0 to 255)"
)


def _unreached(host: str) -> bool:
    """Whether ``host``, as written, is this computer under another name than localhost, 127.0.0.1 or [::1]: a name
    under ``.localhost``, numbers the system's resolver reads as an address (``127.1``, ``2130706433``, ``0x7f000001``,
    as E3's ``oidc._loopback`` counts them), another address of 127.0.0.0/8, or [::1] written otherwise. Facts of the
    address alone, the same at every read; what E0b's egress says of a literal address is asked when the outputs are
    saved (``siem_problem``), since its answer changes with what it learns (NAT64 prefixes) and with Python's registry,
    and a read never refuses settings saved before (fix round 1)."""
    name = _host(host)
    if name in _LOOPBACK:
        return False
    if name.endswith(".localhost"):
        return True
    try:
        found = ipaddress.ip_address(name)
    except ValueError:
        return _NUMBERS.fullmatch(name) is not None
    if isinstance(found, ipaddress.IPv6Address):
        if found.ipv4_mapped is None:
            return int(found) == 1
        found = found.ipv4_mapped
    return found in _THIS_COMPUTER


def _address_checks(
    url: str, headers: Mapping[str, str], *, kind: str, schemes: tuple[str, ...], loopback_http: bool
) -> None:
    """The checks of an address the server reaches (E4's outputs, E6's url sources and Microsoft's addresses): visible
    ASCII, no `{secret}` in the host, a port that is a number, no fragment, a scheme of ``schemes`` (``("tls",)``: a
    syslog output's, with no path or query; else http to this computer too when ``loopback_http``), no user or password,
    a host of the usual shape that is not this computer under another name, no token written in the path or the query,
    headers of names Latet does not set itself, and a credential in a header or the query written `{secret}`.
    ValueError in fixed words: never what was given."""
    if not all(33 <= ord(c) <= 126 for c in url):
        raise ValueError("a url is visible ASCII with no space (an international name in its xn-- form)")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
    except ValueError:
        raise ValueError(_UNREADABLE) from None
    if "{secret}" in parts.netloc:  # a secret in a name goes to every resolver on the way
        raise ValueError("{secret} goes in the path, the query or a header, never in its host or its port")
    try:
        parts.port  # noqa: B018 - a port that is not a number from 0 to 65535 raises here
    except ValueError:
        raise ValueError(_UNREADABLE) from None
    if parts.fragment:
        raise ValueError("a url has no fragment (#...): it is never sent")
    if schemes == ("tls",):
        if parts.scheme != "tls" or not hostname or parts.path not in ("", "/") or parts.query:
            raise ValueError("a syslog output's url is tls://host:port (RFC 5424 over TLS)")
    elif not hostname or not (
        parts.scheme in schemes or (loopback_http and parts.scheme == "http" and _host(hostname) in _LOOPBACK)
    ):
        if loopback_http:
            raise ValueError(f"a {kind} output's url is https:// (http:// only to the server itself)")
        raise ValueError(f"a {kind}'s url is https://")
    if parts.username or parts.password:
        raise ValueError("no user or password in the url: write {secret} where the secret goes")
    if ":" in hostname:  # urllib gives a host with a colon only from brackets: an IPv6 address, never a zone
        if "%" in hostname or not _ipv6(hostname):
            raise ValueError(_HOST)
    elif not _HOSTNAME.fullmatch(hostname):
        raise ValueError(_HOST)
    if _unreached(hostname):  # egress's verdict on a literal address: at a save (siem_problem, terms_problem)
        raise ValueError(_UNREACHED)
    if _token_in(kind, hostname, parts.path, parts.query):  # I3, R2-11: kept sealed apart, never in the settings
        raise ValueError("a path or a query that carries a token: write {secret} there and set the token apart")
    seen: set[str] = set()
    for name, value in headers.items():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name):
            raise ValueError("a header's name is letters, digits and hyphens, 64 at most")
        if name.lower() in seen:
            raise ValueError("a header is written once, its case aside")
        seen.add(name.lower())
        if _RESERVED.fullmatch(name):
            whose = f"a {kind}'s" if kind.endswith(" source") else "an output's"  # E4's outputs: their words
            raise ValueError(
                "a header Latet sets itself, or one a proxy or a browser sets (Host, Content-Length, Forwarded, "
                f"X-Forwarded-*, Cookie and the like), is not {whose}"
            )
        if len(value) > _HEADER_LONGEST:
            raise ValueError(f"a header's value is {_HEADER_LONGEST} characters at most")
        if not _visible(value):
            raise ValueError("a header's value is one line of visible ASCII characters")
    for name, value in [*parse_qsl(parts.query, keep_blank_values=True), *headers.items()]:
        if _CREDENTIAL.search(name) and "{secret}" not in value:
            raise ValueError(
                "a header or a query parameter that carries a credential (Authorization, an API key, a token, sig) "
                "is written {secret} in its place, and the secret set apart"
            )


class SiemOutput(BaseModel):
    """An item of ``siem.outputs``: where each event goes as it happens."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$", description="Its name: lowercase letters, digits, hyphens.")
    kind: Literal["webhook", "splunk", "syslog"] = Field(
        description="`webhook` (JSON over HTTPS), `splunk` (its HTTP Event Collector) or `syslog` (RFC 5424 over TLS)."
    )
    url: str = Field(
        max_length=_URL_LONGEST,
        description="Where: `https://…` for a webhook or Splunk (`http://` only to the server itself, 127.0.0.1), "
        "`tls://host:port` for syslog (port 6514 unless given). `{secret}` stands for the secret, in the path or the "
        "query.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        max_length=_HEADERS_MOST,
        description="A webhook's headers. `{secret}` stands for the secret, and a header that carries a credential "
        "(Authorization, an API key, a token) must use it.",
    )
    secret: str | None = Field(
        default=None,
        pattern=r"^siem-[a-z0-9][a-z0-9-]{0,57}$",  # SIEM_SECRETS, within E1's secret_name: 63 characters at most
        description="The name of its secret (a token or key), beginning `siem-`, set with `latet server siem secret` "
        "or in the dashboard, kept sealed in the server's database and never written here. Splunk's is its HEC token; "
        "a webhook's also signs each body (`x-latet-signature`).",
    )
    format: Literal["json", "ndjson"] = Field(
        default="json", description="A webhook's body: `json` (an array of events) or `ndjson` (one event a line)."
    )

    @model_validator(mode="after")
    def _fits(self) -> SiemOutput:
        # Fixed words: a refusal never quotes what the settings were given (an address, a header's name or value may
        # hold anything), urllib's own words included. The address's checks are every kind's (fix round 1's I2).
        if self.kind == "syslog":
            _address_checks(self.url, self.headers, kind=self.kind, schemes=("tls",), loopback_http=False)
        else:
            _address_checks(self.url, self.headers, kind=self.kind, schemes=("https",), loopback_http=True)
        if self.secret is not None and self.secret != f"{SIEM_SECRETS}{self.name}":  # I1: never another output's
            raise ValueError("an output's secret is named after it: siem-<its name>")
        if self.kind != "webhook" and (self.headers or self.format != "json"):
            raise ValueError("headers and format are a webhook's only")
        if self.kind == "splunk" and self.secret is None:
            raise ValueError("a Splunk output needs a secret: its HEC token")
        if "{secret}" in self.url + "".join(self.headers.values()) and self.secret is None:
            raise ValueError("{secret} is used: name the secret")
        return self

    @property
    def local(self) -> bool:
        """At `http://`, or at this computer (localhost, 127.0.0.1, [::1], a trailing dot and the case aside): set with
        `latet server siem set` alone, never from the dashboard (I2, R2-5), since egress lets an output reach the server
        itself where its address names it."""
        parts = urlsplit(self.url)
        return parts.scheme == "http" or _host(parts.hostname or "") in _LOOPBACK

    @property
    def destination(self) -> tuple[str, str, int]:
        """Where it sends, as a scanner would aim it: its scheme, its host (lowercased, without a trailing dot) and its
        port (R2-4 limits the dashboard's saves that add or move one; R2-5)."""
        parts = urlsplit(self.url)
        port = parts.port or {"https": 443, "http": 80, "tls": 6514}.get(parts.scheme, 0)
        return parts.scheme, _host(parts.hostname or ""), port


def siem_bound(output: SiemOutput) -> str:
    """What an output's secret is sealed for (I1; R2-3: E3's ``secret_bound`` binds the whole issuer, since realms and
    tenants share one origin): the output, its kind and its whole address as written, its host lowercased (an IPv6
    address in brackets) and its port said, its path and its query as they are (``{secret}`` left as written), and
    (fix round 1) each header that carries it, its name lowercased and its value as written, in the order of their
    names. Any change of where the secret goes leaves it unopenable until it is set again: a path changed on a shared
    origin (a company's ingress, a collector of many tenants), or a header added that the receiver keeps in its logs
    (`X-Copy: {secret}`), never sends it elsewhere. A url holds no `#` (refused), so the headers' part is told apart."""
    scheme, host, port = output.destination
    parts = urlsplit(output.url)
    netloc = f"[{host}]" if ":" in host else host
    where = f"{scheme}://{netloc}:{port}{parts.path}" + (f"?{parts.query}" if parts.query else "")
    carrying = sorted((name.lower(), value) for name, value in output.headers.items() if "{secret}" in value)
    headers = "#headers=" + json.dumps(carrying, separators=(",", ":")) if carrying else ""
    return f"siem:{output.name}:{output.kind}:{where}{headers}"


def siem_problem(siem: SiemSettings) -> str | None:
    """R10, asked when the outputs are saved and never when the settings are read (fix round 1): why the server never
    reaches an output's literal address, as E0b's egress judges one for an administrator's own service (``inside``:
    the company's network allowed), in fixed words with the output's place; None when it reaches every one. Egress's
    answer changes with what it learns (NAT64 prefixes) and with Python's registry of addresses: an output saved before
    then still reads, and only a save that keeps it is refused. A name is judged when the server connects
    (``siem.pinned``). The twin of E3's ``issuer_problem``; ``with_section`` asks it at every save of the section."""
    from latet.server import egress  # no server extra needed: a computer's install imports it too

    for index, output in enumerate(siem.outputs):
        host = _host(urlsplit(output.url).hostname or "")
        if host in _LOOPBACK:
            continue
        found = egress.literal(host)
        if found is not None and egress.forbidden(found, inside=True):
            return f"siem.outputs.{index}: {_UNREACHED}"
    return None


class SiemSettings(BaseModel):
    """``siem``: where each event goes as it happens, its outputs each with a name of its own."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    outputs: list[SiemOutput] = Field(default_factory=list, max_length=50, description="Where each event goes.")

    @field_validator("outputs")
    @classmethod
    def _one_name_each(cls, outputs: list[SiemOutput]) -> list[SiemOutput]:
        names = [output.name for output in outputs]
        if len(set(names)) != len(names):
            raise ValueError("each SIEM output has a name of its own")
        return outputs


class ApprovalsSettings(BaseModel):
    """Who decides on what lowers protection on a Latet server (E5): whoever's settings they are (``self``; an API key's
    change, the people who control the key, or the admins), or the company's security team (``security``)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    changes: Literal["self", "security"] = Field(
        default="self",
        description="A change that lowers protection: approved by whoever's settings they are (self; an API key's by "
        "another person who controls it, or the admins), or by the security team (security), for every person and API "
        "key. With security, settings that arrive from the server's shared settings stay each person's to approve when "
        "the shared settings alone lowered them; when the person's own settings lower protection too, they go to the "
        "security team, as they do for a person whose last approval a Latet before central approvals kept, until their "
        "settings are next read with nothing waiting or they next approve a change.",
    )
    live_review: Literal["self", "security"] = Field(
        default="self",
        description="The Live Reviewer's texts: approved by whoever sends them (self), or by the security team "
        "(security), who then see each text, values included.",
    )


# The term sync's sources (E6): each reads its company's records on a schedule, on a Latet server, and maps their
# fields to term types. A source names no secret: its own is `term-sync-<its name>`, set with `latet server terms
# secret`, sealed in the server's database, bound to where it goes (terms_bound). A file source reads inside the
# server's term-sync folder alone (<data>/term-sync, the instances' shared volume).
TERM_SYNC_SECRETS: Final = "term-sync-"
TERM_FILES: Final = "term-sync"
# The refusal (409 `sources-changed`) of a change of the sources, or of a secret, made from sources read before another
# change: `latet server terms` tells it from other refusals by these words.
SOURCES_CHANGED: Final = "the sources changed since they were read: read them again"
SOURCE_NAME: Final = r"^[a-z0-9][a-z0-9-]{0,39}$"  # within E1's secret names once term-sync- is before it
TYPE: Final = re.compile(r"[A-Z][A-Z0-9_]{1,31}")  # a term's type: CLIENT, PERSON, PROJECT...
TEMPLATE: Final = re.compile(r"\{([^{}]{1,64})\}")  # "{givenName} {sn}": several fields made one term
ENCODING_NAME: Final = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # utf-8, cp1252, ISO-8859-15, utf_16_le...
_FILTER_LONGEST = 4096
_DN_LONGEST = 1024


class BaseSource(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(pattern=SOURCE_NAME, description="Its name: lowercase letters, digits and hyphens, 40 at most.")
    every_minutes: int = Field(default=60, ge=5, le=10080, description="Minutes between two syncs, 5 to 10080.")
    mapping: dict[str, str] = Field(
        min_length=1, max_length=32,
        description='Which field of a record becomes a term of which type: `{company: CLIENT}`; a field may be a '
        'template of several, `"{givenName} {sn}": PERSON`.',
    )  # fmt: skip
    exclude: list[str] = Field(
        default_factory=list, max_length=1000,
        description="Values never taken as terms, in any case: ordinary words, such as a group named `Sales`.",
    )  # fmt: skip
    min_length: int = Field(
        default=4, ge=2, le=50,
        description="The shortest value taken as a term; a person's name from 3 whatever this says.",
    )  # fmt: skip

    @field_validator("mapping")
    @classmethod
    def _types(cls, mapping: dict[str, str]) -> dict[str, str]:
        for field, etype in mapping.items():
            if not field.strip() or len(field) > 256 or ("{" in field and not TEMPLATE.search(field)):
                raise ValueError('a field\'s name, or a template such as "{givenName} {sn}"')
            if not TYPE.fullmatch(etype):
                raise ValueError("a type is a name in capitals, such as CLIENT or PERSON")
        return mapping

    @field_validator("exclude")
    @classmethod
    def _excluded(cls, values: list[str]) -> list[str]:
        if any(len(value) > 200 for value in values):
            raise ValueError("an excluded value is 200 characters at most")
        return values


class _Document(BaseSource):
    format: Literal["csv", "json", "yaml"] | None = Field(
        default=None,
        description="Not set: from the extension of the file or of the address's path, else JSON; an answer whose "
        "content type names another format is refused.",
    )
    records: str | None = Field(
        default=None, max_length=256,
        description="Where the list of records is in a JSON or YAML document (`data.items`); not set: the document.",
    )  # fmt: skip
    delimiter: str = Field(default=",", min_length=1, max_length=1, description="A CSV file's field separator.")
    encoding: str = Field(default="utf-8", description="The text's encoding: `utf-8` (a BOM is skipped), `cp1252`...")

    @field_validator("records")
    @classmethod
    def _where(cls, records: str | None) -> str | None:
        """Printable characters alone, Unicode's included (a key such as `données`): no control character, no format
        one (a bidirectional override, a zero-width space), no space but ASCII's, no lone surrogate, which could not
        reach a run's reason or a log safely. A run's reason names where the records were looked for only when it is
        visible ASCII (``records._parsed``)."""
        if records is not None and (not records or not records.isprintable()):
            raise ValueError("records are found at keys of printable characters, joined by dots: data.items")
        return records

    @field_validator("delimiter")
    @classmethod
    def _separator(cls, delimiter: str) -> str:
        """One the CSV reader takes: a visible character but the quote, or a tab."""
        if delimiter != "\t" and (delimiter == '"' or not _visible(delimiter)):
            raise ValueError('a CSV field separator is one visible character other than ", or a tab')
        return delimiter

    @field_validator("encoding")
    @classmethod
    def _known(cls, encoding: str) -> str:
        """A text encoding this Python decodes with (never zlib, base64 or rot13, which bytes.decode refuses, nor
        `undefined`, which decodes nothing), named in letters, digits, `_`, `.` and `-`: a reason may name it."""
        if ENCODING_NAME.fullmatch(encoding):
            try:
                if codecs.lookup(encoding)._is_text_encoding:
                    codecs.getincrementaldecoder(encoding)().decode(b"", final=True)
                    return encoding
            except (LookupError, ValueError):  # UnicodeError is a ValueError
                pass
        raise ValueError("not a text encoding Python knows, such as utf-8 or cp1252")


def _inside(path: str) -> str:
    """A file's name inside the term-sync folder, with no folder of its own (B-M8: no folder to swap for a link between
    the check and the open), no drive, nothing but visible characters."""
    if (
        not path or len(path) > 255 or "/" in path or "\\" in path or ":" in path or path in (".", "..")
        or not _visible(path)
    ):  # fmt: skip
        raise ValueError(f"the name of a file inside the server's {TERM_FILES} folder, with no folder: customers.csv")
    return path


class FileSource(_Document):
    kind: Literal["file"]
    path: str = Field(description=f"The file, inside the server's `{TERM_FILES}` folder (its data folder's).")

    @field_validator("path")
    @classmethod
    def _path(cls, path: str) -> str:
        return _inside(path)


class UrlSource(_Document):
    kind: Literal["url"]
    url: str = Field(
        max_length=_URL_LONGEST, description="Where: https://… (a local export goes in the term-sync folder)."
    )
    headers: dict[str, str] = Field(
        default_factory=dict, max_length=_HEADERS_MOST,
        description="Headers sent with the request; `{secret}` stands for the source's secret.",
    )  # fmt: skip

    @model_validator(mode="after")
    def _fits(self) -> UrlSource:
        # https alone, the server's own loopback refused too (R41): a local export goes in the term-sync folder
        _address_checks(self.url, self.headers, kind="url source", schemes=("https",), loopback_http=False)
        return self


class GraphSource(BaseSource):
    kind: Literal["graph"]
    tenant: str = Field(
        pattern=r"^[A-Za-z0-9.-]{1,256}$", description="The Microsoft Entra tenant: its id or a domain."
    )
    client_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$", description="The application (client) id of Latet's app.")
    objects: Literal["users", "groups"] = Field(default="users", description="`users` or `groups`.")
    group: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9-]{1,64}$", description="With `users`: this group's members (its id)."
    )
    filter: str | None = Field(default=None, max_length=_FILTER_LONGEST, description="An OData filter Graph applies.")
    login_url: str = Field(
        default="https://login.microsoftonline.com", max_length=_URL_LONGEST, description="Entra's sign-in address."
    )
    graph_url: str = Field(
        default="https://graph.microsoft.com", max_length=_URL_LONGEST, description="Microsoft Graph's address."
    )

    @model_validator(mode="after")
    def _fits(self) -> GraphSource:
        if not self.tenant.strip("."):  # a dot segment (`.`, `..`): the token's request would leave the tenant's path
            raise ValueError("a tenant is its id or a domain, such as acme.onmicrosoft.com")
        for url in (self.login_url, self.graph_url):
            _address_checks(url, {}, kind="graph source", schemes=("https",), loopback_http=False)
            # A query or a fragment, even an empty one (Task 8's review), as a directory's: what follows the origin
            # would move the token's request and the pages to another path of the host.
            if urlsplit(url).path not in ("", "/") or any(sign in url for sign in "?#"):
                raise ValueError("login_url and graph_url are an origin: https://host, with no path, query or fragment")
            if _host(urlsplit(url).hostname or "") in _LOOPBACK:  # egress refuses it to a public service anyway
                raise ValueError("login_url and graph_url are Microsoft's public addresses, never the server itself")
        if self.group and self.objects != "users":
            raise ValueError("`group` reads a group's members: with `objects: users` only")
        if self.filter is not None and not _visible(self.filter):
            raise ValueError("a filter is one line of visible characters")
        return self


class LdapSource(BaseSource):
    kind: Literal["ldap"]
    url: str = Field(
        max_length=_URL_LONGEST, description="`ldaps://dc1.acme.lan`, or `ldap://` with `start_tls`; a port may follow."
    )
    start_tls: bool = Field(default=True, description="With `ldap://`: encrypt with STARTTLS before signing in.")
    bind: str | None = Field(
        default=None, max_length=_DN_LONGEST, description="Who Latet signs in as; none: anonymous."
    )
    base: str = Field(min_length=1, max_length=_DN_LONGEST, description="Where the search starts.")
    filter: str = Field(default="(objectClass=person)", max_length=_FILTER_LONGEST, description="An LDAP filter.")
    certificate: str | None = Field(
        default=None, description=f"A certificate authority's PEM file inside the server's `{TERM_FILES}` folder."
    )

    @model_validator(mode="after")
    def _fits(self) -> LdapSource:
        if not all(33 <= ord(c) <= 126 for c in self.url):  # urllib drops a tab or a line break: the url kept holds it
            raise ValueError("a url is visible ASCII with no space (an international name in its xn-- form)")
        try:
            parts = urlsplit(self.url)
            parts.port  # noqa: B018 - a port that is not a number raises here
        except ValueError:
            raise ValueError(_UNREADABLE) from None
        host = parts.hostname or ""
        if "@" in parts.netloc:  # an empty user too: urllib's and the directory library's parsers must agree
            raise ValueError("no user or password in the address: `bind` and the source's secret")
        if (
            parts.scheme not in ("ldaps", "ldap")
            or not host
            or parts.path not in ("", "/")
            or any(sign in self.url for sign in "?#")
        ):  # a query or a fragment, even an empty one
            raise ValueError("an address such as ldaps://dc1.acme.lan, with no path, query or fragment")
        if "%" in host or not (_ipv6(host) or _HOSTNAME.fullmatch(host)) or _unreached(host):  # a zone: none
            raise ValueError(_UNREACHED if _unreached(host) else _HOST)
        if parts.scheme == "ldap" and not self.start_tls and _host(host) not in _LOOPBACK:
            raise ValueError("ldaps://, or ldap:// with start_tls: names and the password cross the network (TLS)")
        for text in (self.bind or "", self.base, self.filter):
            if not _visible(text):
                raise ValueError("bind, base and filter are one line of visible characters")
        if self.filter.count("(") != self.filter.count(")") or not self.filter.startswith("("):
            raise ValueError("a filter in parentheses, each one closed: (objectClass=person)")
        if self.certificate is not None:
            _inside(self.certificate)
        return self


TermSource = Annotated[FileSource | UrlSource | GraphSource | LdapSource, Field(discriminator="kind")]
_KINDS = ("file", "url", "graph", "ldap")


class TermSyncSettings(BaseModel):
    """``term_sync``: every source of the terms your company syncs (Latet Enterprise, on a server), each read on a
    schedule, its fields mapped to term types."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    sources: list[TermSource] = Field(default_factory=list, max_length=20, description="The sources.")

    @field_validator("sources", mode="before")
    @classmethod
    def _kinds(cls, sources: Any) -> Any:
        """A kind no source has, said in fixed words: pydantic's for a tagged union quote the tag given."""
        if isinstance(sources, list) and any(
            isinstance(source, Mapping) and "kind" in source and source["kind"] not in _KINDS for source in sources
        ):
            raise ValueError("a source's kind is file, url, graph or ldap")
        return sources

    @field_validator("sources")
    @classmethod
    def _one_name_each(cls, sources: list[TermSource]) -> list[TermSource]:
        names = [source.name for source in sources]
        if len(set(names)) != len(names):
            raise ValueError("each source has a name of its own")
        return sources


def source_secret(source: TermSource) -> str:
    """The name of ``source``'s own secret among the server's: never another source's (R11)."""
    return f"{TERM_SYNC_SECRETS}{source.name}"


def needs_secret(source: TermSource) -> bool:
    """Whether ``source`` reads with a secret: a graph source always, an ldap source that signs in, a url source that
    writes `{secret}`."""
    if isinstance(source, GraphSource):
        return True
    if isinstance(source, LdapSource):
        return source.bind is not None
    if isinstance(source, UrlSource):
        return "{secret}" in source.url + "".join(source.headers.values())
    return False


def source_local(source: TermSource) -> bool:
    """Whether ``source`` reaches the server's own loopback (a url or ldap source at localhost, 127.0.0.1 or [::1], a
    trailing dot and the case aside): set, with its secret, by `latet server terms` alone, never from the dashboard,
    as E4's ``SiemOutput.local``."""
    if isinstance(source, UrlSource | LdapSource):
        return _host(urlsplit(source.url).hostname or "") in _LOOPBACK
    return False


def default_port(scheme: str) -> int:
    """A scheme's own port, where an address of a term source writes none: what its destinations count, what its secret
    is kept for, and where a directory and Microsoft Graph are reached alike (the final review's M2); 0 for a scheme no
    source has."""
    return _PORTS.get(scheme, 0)


_PORTS: Final = {"https": 443, "http": 80, "ldaps": 636, "ldap": 389}


def source_destinations(source: TermSource) -> frozenset[tuple[str, str, int]]:
    """Where ``source`` connects, as a scanner would aim it: each address's scheme, host and port (a graph source's
    two addresses; none for a file), which the dashboard's count of sources added or moved weighs (R22)."""
    if isinstance(source, GraphSource):
        urls = [source.login_url, source.graph_url]
    elif isinstance(source, UrlSource | LdapSource):
        urls = [source.url]
    else:
        return frozenset()
    found = set()
    for url in urls:
        parts = urlsplit(url)
        found.add((parts.scheme, _host(parts.hostname or ""), parts.port or default_port(parts.scheme)))
    return frozenset(found)


def destination_addresses(source: TermSource) -> list[str]:
    """Where ``source`` connects, as people read it: each of its destinations (``source_destinations``) as
    `scheme://host:port`, an IPv6 host in brackets, sorted; none for a file. What `latet server terms secret` and the
    Term sync page's secret dialog say before they ask for a secret (the final review's M1)."""
    return [
        f"{scheme}://[{host}]:{port}" if ":" in host else f"{scheme}://{host}:{port}"
        for scheme, host, port in sorted(source_destinations(source))
    ]


def mapped_fields(source: TermSource) -> list[str]:
    """The fields ``source`` reads, by name, sorted: each field its mapping names, each of a template's (LDAP's
    attributes, Graph's `$select`)."""
    return sorted({name for field in source.mapping for name in (TEMPLATE.findall(field) or [field])})


def _what_it_reads(source: TermSource) -> dict[str, Any]:
    """What ``source`` reads with its secret (the design security review's B-I3 and B-M6): changed, the secret must be
    set again, so that whoever edits the sources never reuses it to read other data with it. Never what is not read
    with it (the schedule, `exclude`, `min_length`, a field's type)."""
    scope: dict[str, Any] = {"fields": mapped_fields(source)}
    if isinstance(source, UrlSource):  # every header too, names and values (B-R2): one can change what is answered
        scope |= {"records": source.records, "headers": sorted([n.lower(), v] for n, v in source.headers.items())}
    elif isinstance(source, GraphSource):
        scope |= {"objects": source.objects, "group": source.group, "filter": source.filter,
                  "graph_url": source.graph_url.rstrip("/")}  # fmt: skip
    elif isinstance(source, LdapSource):
        scope |= {"base": source.base, "filter": source.filter}
    return scope


def _where_it_goes(source: TermSource) -> list[Any]:
    """Each place ``source``'s secret goes, part by part: a url's scheme, host, port, path, query and the headers that
    carry it; a graph's sign-in origin, tenant and application; a directory's scheme, host, port, the DN it signs in as
    and the authority it trusts (fix round 1: who may receive the bind password). None for a file."""
    if isinstance(source, UrlSource):
        parts = urlsplit(source.url)
        carrying = sorted([name.lower(), value] for name, value in source.headers.items() if "{secret}" in value)
        port = parts.port or default_port(parts.scheme)  # https alone: 443
        return [parts.scheme, _host(parts.hostname or ""), port, parts.path, parts.query, carrying]
    if isinstance(source, GraphSource):
        return [source.login_url.rstrip("/"), source.tenant, source.client_id]
    if isinstance(source, LdapSource):
        parts = urlsplit(source.url)
        port = parts.port or default_port(parts.scheme)  # ldaps or ldap alone: 636 or 389
        return [parts.scheme, _host(parts.hostname or ""), port, source.bind, source.certificate]
    return []


def terms_bound(source: TermSource) -> str:
    """What ``source``'s secret is sealed for (as E4's ``siem_bound``): its name, its kind, every place the secret goes
    and what it reads with it, so that a source changed there cannot open it until it is set again, never sending it
    elsewhere nor reading other data with it. One JSON array of those parts (fix round 1's Important 1): written as a
    joined string, an IPv6 host, a port and a DN holding colons could read as another host, port and DN."""
    bound = ["term-sync", source.name, source.kind, _where_it_goes(source), _what_it_reads(source)]
    return json.dumps(bound, separators=(",", ":"), sort_keys=True)


def terms_problem(settings: TermSyncSettings) -> str | None:
    """Asked when the sources are saved, never when they are read (as ``siem_problem``): why the server never reaches a
    source's literal address, as E0b's egress judges it (a url or a directory inside the company's network allowed,
    Microsoft's clouds public); None when it reaches every one. A name is judged when the server connects."""
    from latet.server import egress

    for index, source in enumerate(settings.sources):
        hosts = (
            [(urlsplit(source.url).hostname or "", True)] if isinstance(source, UrlSource | LdapSource)
            else [(urlsplit(u).hostname or "", False) for u in (source.login_url, source.graph_url)]
            if isinstance(source, GraphSource) else []
        )  # fmt: skip
        for host, inside in hosts:
            if _host(host) in _LOOPBACK:
                continue
            found = egress.literal(_host(host))
            if found is not None and egress.forbidden(found, inside=inside):
                return f"term_sync.sources.{index}: {_UNREACHED}"
    return None


class EnterpriseSettings(BaseModel):
    """The Enterprise settings, a field for each section: the sign-in's (E3) first; each later part adds its own
    here."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    sso: SsoSettings | None = None
    reports: ReportSettings = ReportSettings()  # E4: how people appear in reports, and how long the log keeps them
    siem: SiemSettings = SiemSettings()  # E4: where each event goes as it happens
    # E5: who decides on what lowers protection, and on the Live Reviewer's texts
    approvals: ApprovalsSettings = ApprovalsSettings()
    term_sync: TermSyncSettings = TermSyncSettings()  # E6: where the terms your company syncs come from


def _field_names() -> frozenset[str]:
    """The name of every field of the settings, at any depth: what a place shows. Read at each refusal, as the sections
    are those of the settings in use."""
    schema = EnterpriseSettings.model_json_schema()
    found = set(schema.get("properties", {}))
    for model in schema.get("$defs", {}).values():
        found |= set(model.get("properties", {}))
    return frozenset(found)


def _problems(exc: ValidationError) -> str:
    """What is wrong, each by its place and pydantic's words, which quote nothing they were given. A section this
    version does not have is named, as a typo should be; below a section, a name no field has is shown as ``*``, as
    ``latet.config.place_of`` shows it: it is a key the settings were given, which may be anything."""
    names = _field_names()
    said: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = error["loc"]
        if error["type"] == "extra_forbidden" and len(loc) == 1:
            said.append(f"there is no Enterprise setting named {loc[0]!r}")
            continue
        place = ".".join(str(p) if isinstance(p, int) or p in names else "*" for p in loc) or "the settings"
        said.append(f"{place}: {error['msg'].removeprefix('Value error, ')}")
    return "; ".join(said)


def settings_from(sections: Mapping[str, Any], *, skip_unknown: bool = False) -> EnterpriseSettings:
    """The settings these sections make (the defaults for the others). A section this version does not know is
    refused, or skipped with a warning (``skip_unknown``: a server's rows, which a newer version may have left)."""
    known = EnterpriseSettings.model_fields
    if skip_unknown:
        for name in sorted(set(sections) - set(known)):
            log.warning("the Enterprise setting %r is not known to this version of Latet: skipped", name)
        sections = {name: value for name, value in sections.items() if name in known}
    try:
        return EnterpriseSettings.model_validate(dict(sections))
    except ValidationError as exc:
        problems = _problems(exc)
    # Raised once pydantic's error is handled, so that no traceback shows it with this one (it holds what was given).
    raise EnterpriseSettingsError(problems)


def with_section(settings: EnterpriseSettings, name: str, value: Mapping[str, Any] | None) -> EnterpriseSettings:
    """``settings`` with the section ``name`` set to ``value`` (None: back to its defaults). UnknownSection, or
    EnterpriseSettingsError saying what is wrong in ``value`` (a value that is not a mapping included; for ``siem``, an
    output at an address the server never reaches: ``siem_problem``; for ``term_sync``, a source at one:
    ``terms_problem``)."""
    if name not in EnterpriseSettings.model_fields:
        raise UnknownSection(name)
    sections = settings.model_dump(mode="json", exclude_defaults=True)
    if value is None:
        sections.pop(name, None)
    else:
        sections[name] = dict(value) if isinstance(value, Mapping) else value
    changed = settings_from(sections)
    # R10 at a save of the outputs alone (fix round 1): egress's verdict, which a read never asks.
    if name == "siem" and value is not None and (problem := siem_problem(changed.siem)) is not None:
        raise EnterpriseSettingsError(problem)
    if name == "term_sync" and value is not None and (problem := terms_problem(changed.term_sync)) is not None:
        raise EnterpriseSettingsError(problem)
    return changed


def section_value(settings: EnterpriseSettings, name: str) -> Any:
    """The section ``name`` of ``settings``, as JSON; UnknownSection when the settings have none of that name."""
    if name not in type(settings).model_fields:
        raise UnknownSection(name)
    return settings.model_dump(mode="json", include={name})[name]


class EnterpriseSettingsStore(Protocol):
    def load(self) -> EnterpriseSettings: ...
    def where(self) -> str | None: ...  # where they are read: "server", or None (the defaults)
    def set_section(
        self,
        name: str,
        value: Mapping[str, Any] | None,
        *,
        actor: str,
        address: str | None = None,
        action: str | None = None,
    ) -> EnterpriseSettings: ...
    def drop(self) -> None: ...  # read them again at the next use
