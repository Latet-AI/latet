"""Structural secret detectors: assignments with sensitive names, PEM private keys, credentials in
URLs, JWTs and bearer tokens. They complement gitleaks where its rules require entropy or quotes."""

from __future__ import annotations

import bisect
import functools
import re
from collections.abc import Iterator

import re2

from latet.engine.detectors.base import DetectContext, group
from latet.engine.types import EntityClass, Span

# --- KEY=VALUE ---------------------------------------------------------------------------------

# One assignment per line: .env, ini, properties, YAML, JSON members, PowerShell $env:, and simple
# declarations in code. A leading "  12\t" or "12→" is the line-number prefix Claude Code adds to files
# it reads. The name must contain a sensitive-looking word (checked by RE2, so the many ordinary
# assignment lines of code cost nothing in Python). Group 6 keeps the trailing punctuation: a "," or ")"
# there means a call argument in code.
_ASSIGNMENT = re2.compile(
    r"(?m)^[ \t]*(?:\d+(?:\t|→))?[ \t]*"
    r"(?:(?:export|set|const|let|var|final|static|private|public|protected|readonly|val|string|String)[ \t]+)*"
    r"(?:\$env:)?"
    r"([\"']?)((?:[A-Za-z_][A-Za-z0-9_.\-]{0,80}?)?(?i:pass|pwd|pw|secret|token|key|cred|auth|bearer|cookie|salt|dsn|conn)"
    r"[A-Za-z0-9_.\-]{0,80})([\"']?)"
    r"[ \t]*(:=|=|:)[ \t]*"
    r"([^\r\n]*?)([ \t,;)]*)\r?$"
)

# A ``key=value`` pair wherever it stands in a line, not only where the line begins with it: after other words in a log
# (``INFO login ok password=...``), in parentheses, in a URL's query (``?token=...&b=1``), beside other pairs. The key
# holds a word of a secret's name as above, begins the line or follows a character no name holds, and is followed by
# the ``=`` right away (not ``==``), after ``--`` or ``-`` where a command line has it (``--password=...``); the value
# is quoted, or runs to a space, a delimiter or a backtick (the end of a markdown code span). No space around the
# ``=``: see ``_INLINE_QUOTED``. RE2 has no look-behind: the character before the key is read with it.
_INLINE_PAIR = re2.compile(
    r"(?m)(?:^|[^A-Za-z0-9_.\-/])(?:--?)?"
    r"((?:[A-Za-z_][A-Za-z0-9_.\-]{0,80}?)?(?i:pass|pwd|pw|secret|token|key|cred|auth|bearer|cookie|salt|dsn|conn)"
    r"[A-Za-z0-9_.\-]{0,80})"
    r"=(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s&,;\"'`<>()\[\]{}=][^\s&,;\"'`<>()\[\]{}]*)"
)
# After a strong name (a password, a passphrase, a secret, an API, private, access or auth key or token), a quoted
# literal behind a colon or a spaced ``=``, wherever it stands in a line: ``{ username: 'Alice', password: 's3cr3t' }``,
# ``SET password = '...'``. Only a quoted literal: an unquoted value there is a type (``password: Optional[str]``), a
# variable (``user, password = proxy_auth``) or a comparison, far more often than a secret.
_INLINE_QUOTED = re2.compile(
    r"(?m)(?:^|[^A-Za-z0-9_.\-/])"
    r"((?:[A-Za-z_][A-Za-z0-9_.\-]{0,80}?)?(?i:password|passwd|pwd|passphrase|secret|api[_.\-]?key|"
    r"private[_.\-]?key|secret[_.\-]?key|access[_.\-]?token|auth[_.\-]?token))[\"']?"
    r"[ \t]*(:=|:|=)[ \t]*(\"[^\"\r\n]*\"|'[^'\r\n]*')"
)
# What a quoted value holds when it is a piece of a text and no literal: a format or a template field (`%s`, `%(name)s`,
# `{name}`).
_FORMAT = re.compile(r"%[sdrifx(]|\{\w*\}")

# Matched as whole words of the name (snake, kebab, dot or camel case), or as two adjacent words.
_STRONG = frozenset(
    {
        "password", "passwd", "passphrase", "secret", "secrets", "token", "credential", "credentials",
        "apikey", "privatekey", "accesskey", "secretkey", "clientsecret", "connectionstring", "connstr",
        "sessionkey", "masterkey", "encryptionkey", "signingkey", "bearer", "apitoken", "authtoken",
    }
)  # fmt: skip
_EXACT = frozenset({"pwd", "pw", "pass", "dsn", "salt", "cookie", "creds", "auth"})
_PASSWORDISH = frozenset({"password", "passwd", "passphrase", "pwd", "pass", "pw"})
# Names of a whole connection string: only its credentials are secret (see connection_secrets). A
# DSN stays a secret as a whole: Sentry's carries its key as the user of a URL.
_CONNECTION = frozenset({"connectionstring", "connstr"})
# A last word that makes the name describe configuration around a secret, not the secret itself.
_NOT_SECRET_LAST = frozenset(
    {
        "url", "uri", "endpoint", "host", "hostname", "port", "path", "file", "dir", "name", "names", "id",
        "ids", "type", "types", "method", "mode", "provider", "enabled", "required", "count", "limit",
        "length", "len", "max", "min", "ttl", "timeout", "expiry", "expiration", "expires", "lifetime",
        "duration", "version", "alg", "algorithm", "policy", "hint", "label", "prompt", "field", "header",
        "prefix", "suffix", "hdr", "headers", "scope", "scopes", "format", "rounds", "strength", "regex",
        "regexp", "pattern",
        "domain", "issuer", "audience", "location", "region", "filter", "parser", "class", "kind", "index",
        "idx", "size", "store", "storage", "manager", "factory", "handler", "service", "list", "map", "cache",
    }
)  # fmt: skip
# The last word of a name that says what a check found about a secret (`auth_status`, `token_error`,
# `password_changed`). The name still names a secret: only a value of the shape of a status (``_STATUS_SHAPE``) is left.
_STATUS_LAST = frozenset({"status", "state", "result", "results", "outcome", "scheme", "changed", "reason", "error"})
# What such a value is: a lowercase word of 20 characters at most with one `-` or `_` at most, a number of 10 digits at
# most (and a fraction of 6), a time, a date (with a time) or a boolean. Longer is a passphrase, a number or a card.
_STATUS_WORD = re2.compile(r"^[a-z]+(?:[-_][a-z]+)?$")
_STATUS_WORD_LENGTH = 20
_STATUS_SHAPE = re2.compile(
    r"^(?:[+-]?\d{1,10}(?:[.,]\d{1,6})?|TRUE|FALSE|True|False|"
    r"\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?|"
    r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})"
    r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?)$"
)


def _status_shaped(value: str) -> bool:
    """Whether ``value`` has the shape of a status (``_STATUS_LAST``): see ``_STATUS_SHAPE``."""
    if len(value) <= _STATUS_WORD_LENGTH and _STATUS_WORD.match(value):
        return True
    return _STATUS_SHAPE.match(value) is not None


# The last word of a command-line option that takes the name of something: `--pass-arg=asyncify-imports@env.x`,
# `--auth-flags=verbose`. A pair after `-` or `--` in the middle of a line (``_INLINE_PAIR``) with such a name is none.
_OPTION_LAST = frozenset({"arg", "args", "argument", "arguments", "option", "options", "flag", "flags"})
# A lowercase word that says how a check went, which a value after a secret's name can be (`auth=failed`,
# `token=expired`): no secret, as `changeme` is none.
_STATUS = re2.compile(
    r"^(?:ok|fail|failed|failure|denied|refused|rejected|accepted|granted|success|succeeded|error|expired|missing|"
    r"invalid|valid|required|present|absent|revoked|rotated|refreshed|pending|unknown|anonymous|unauthorized|"
    r"forbidden|enabled|disabled|active|inactive|skipped|passed)$"
)
_NOT_SECRET_WORDS = frozenset(
    {"author", "authors", "authority", "tokenizer", "tokenize", "tokenized", "tokens", "oauth2"}
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[_.\-\s]+")

_PLACEHOLDER = re2.compile(
    r"(?i)^(?:x+|\*+|\.{3,}|-+|_+|null|none|nil|true|false|undefined|empty|changeme|change_me|example|"
    r"placeholder|redacted|secret|password|passwd|token|todo|tbd|xxx+|your[_\-. ].*|<[^>]*>|\[[^\]]*\]|"
    r"\{\{.*\}\}|\$\{.*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%|\{env:[^}]*\}|\{file:[^}]*\})$"
)
_CODE_REFERENCE = re2.compile(
    r"^(?:process\.env|os\.environ|os\.getenv|getenv|env\(|config\.|settings\.|self\.|this\.|ENV\[|System\.getenv)"
)
_IDENTIFIER = re2.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_DOTTED_IDENTIFIER = re2.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$")
_DOTTED_NUMBER = re2.compile(r"^\d+(?:\.\d+)+$")
_REGEX_LITERAL = re2.compile(r"^/.+/[a-z]*$")
_NUMBER = re2.compile(r"^\d{1,6}$")
_CODE_CHARS = set("()[]{}")
_JS_EXPRESSION = re2.compile(r"\?\.|^!!?[A-Za-z_$]|&&|\|\||=>")
_TYPE_NAMES = frozenset(
    {"str", "string", "int", "integer", "bool", "boolean", "float", "double", "number", "bytes", "list",
     "dict", "set", "tuple", "any", "object", "none", "char", "void", "optional", "unknown", "never"}
)  # fmt: skip


def _words(key: str) -> list[str]:
    words: list[str] = []
    for part in _SEPARATORS.split(key):
        words += [w.lower() for w in _CAMEL.split(part) if w]
    return words


@functools.lru_cache(maxsize=4096)
def _status_name(key: str) -> bool:
    """Whether the last word of ``key`` says what a check found about a secret (``_STATUS_LAST``)."""
    words = _words(key.rsplit(".", 1)[-1].replace("[]", ""))
    return bool(words) and words[-1] in _STATUS_LAST


@functools.lru_cache(maxsize=4096)  # a log repeats a few names a million times
def _key_kind(key: str) -> str | None:
    """``"password"``, ``"secret"``, ``"connection"`` (a connection string) or None, from the words
    of a variable name.

    For a dotted name (``this.currentToken.data``, ``spring.datasource.password``) only the last
    segment says what the value is.
    """
    words = _words(key.rsplit(".", 1)[-1])
    if not words or any(w in _NOT_SECRET_WORDS for w in words) or words[-1] in _NOT_SECRET_LAST:
        return None
    hits: list[str] = []
    for i, word in enumerate(words):
        pair = word + words[i + 1] if i + 1 < len(words) else ""
        if word in _STRONG or word in _EXACT:
            hits.append(word)
        elif pair in _STRONG:
            hits.append(pair)
        elif len(word) > 6:  # lowercase compounds: dbpassword, githubtoken
            hits += [s for s in _STRONG if len(s) >= 5 and word.endswith(s)]
    if not hits:
        return None
    if any(h in _PASSWORDISH or h.endswith("password") for h in hits):
        return "password"
    return "connection" if all(h in _CONNECTION for h in hits) else "secret"


_LINE_COMMENT = ("#", "//", "--", ";", "/*")


def _strip_comment(raw: str) -> str:
    """Drop a line comment after the value: ``"s3cret"  # rotate``, ``"s3cret";  // x``, ``s3cret # x``."""
    if raw[:1] and raw[0] in "\"'`":
        end = raw.find(raw[0], 1)
        while end > 0 and raw[end - 1] == "\\":
            end = raw.find(raw[0], end + 1)
        if end > 0 and raw[end + 1 :].strip().startswith(_LINE_COMMENT):
            return raw[: end + 1]
        return raw
    cut = raw.find(" #")  # .env and YAML: a comment starts after a space
    return raw[:cut].rstrip() if cut > 0 else raw


def _unquote(raw: str) -> tuple[str, int, bool]:
    """Strip one pair of matching quotes. Returns (value, offset of value in raw, was_quoted).

    ``"Basic " + encode(x)`` starts and ends with a quote but is an expression, not a string.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'`":
        inner = raw[1:-1]
        if raw[0] not in inner.replace("\\" + raw[0], ""):
            return inner, 1, True
    return raw, 0, False


def _code_like(value: str, key: str, quoted: bool) -> bool:
    """Values that are expressions, types or references rather than literal secrets."""
    if _PLACEHOLDER.match(value) or _STATUS.match(value) or _CODE_REFERENCE.match(value):
        return True
    if _status_name(key) and _status_shaped(value):
        return True  # `auth_status=saml`, `password_changed=2026-10-26`: what a check found, under its name
    if _REGEX_LITERAL.match(value) or _DOTTED_NUMBER.match(value) or not any(c.isalnum() for c in value):
        return True
    if value.lower() == key.lower():
        return True
    if _DOTTED_IDENTIFIER.match(value) and not any(c.isdigit() for c in value):
        return True  # module or attribute paths: http.cookies, token.ERRORTOKEN
    if quoted:
        return False
    return bool(_CODE_CHARS.intersection(value)) or value.lower() in _TYPE_NAMES or bool(_JS_EXPRESSION.search(value))


class AssignmentDetector:
    """Values assigned to sensitive names (``DB_PASSWORD=…``, ``"api_key": "…"``, ``$env:TOKEN = …``), and ``name=…``
    pairs wherever they stand in a line."""

    id = "assignment"
    mandatory = True

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if "=" not in text and ":" not in text:
            return
        done: set[tuple[int, int]] = set()  # what this pattern masks, which a pair found again anywhere is not twice
        whole: list[tuple[int, int]] = []  # the lines of a connection string, read whole by this pattern alone
        for m in _ASSIGNMENT.finditer(text):
            key = group(m, 2)
            kind = _key_kind(key)
            if kind is None:
                continue
            if kind == "connection":
                whole.append((m.start(2), m.end()))
            raw_start, raw_end = m.span(5)
            value, offset, quoted = _unquote(_strip_comment(text[raw_start:raw_end]))
            if not value or (value[0] in "{[" and not quoted):
                continue  # a nested object or array follows: its members are matched on their own lines
            if _code_like(value, key, quoted):
                continue
            if _NUMBER.match(value) and kind != "password":
                continue
            if not quoted:
                if " " in value:
                    continue
                spaced = group(m, 4) != ":" and text[m.start(4) - 1 : m.start(4)] == " "
                trailing = group(m, 6)
                in_call = "," in trailing or ")" in trailing
                if _IDENTIFIER.match(value) and not key.isupper() and (spaced or in_call or bool(group(m, 1))):
                    continue  # `token = authToken`, `f(password=pw)`, `"auth": LOG_AUTH`: references in code
            if len(value) < (3 if kind == "password" else 6):
                continue
            start = raw_start + offset
            if kind == "connection":
                parts = connection_secrets(value, start, f"assignment:{key}")
                if parts is not None:
                    yield from parts
                    continue
            etype = "PASSWORD" if kind == "password" else "ASSIGNED_SECRET"
            done.add((start, start + len(value)))
            yield Span(start, start + len(value), etype, EntityClass.SECRET, f"assignment:{key}")
        if "=" in text:
            yield from self._inline(text, done, whole)
        if ":" in text or "=" in text:
            yield from self._quoted(text, done, whole)

    def _inline(self, text: str, done: set[tuple[int, int]], whole: list[tuple[int, int]]) -> Iterator[Span]:
        """The pairs ``key=value`` of a line whose key names a secret, wherever they stand in it (``_INLINE_PAIR``),
        but those the pattern above has masked (``done``, to which these are added) and the pairs inside a connection
        string it reads whole (``whole``)."""
        starts = [start for start, _ in whole]
        for m in _INLINE_PAIR.finditer(text):
            if _within(starts, whole, m.start(1)):
                continue
            key = group(m, 1)
            kind = _key_kind(key)
            if kind is None or kind == "connection":
                continue
            if text[m.start(1) - 1 : m.start(1)] == "-" and _words(key.rsplit(".", 1)[-1])[-1] in _OPTION_LAST:
                continue  # `--pass-arg=...`: an option that takes the name of something
            span = _pair_span(text, key, kind, group(m, 2), m.start(2), m.end())
            if span is not None and (span.start, span.end) not in done:
                done.add((span.start, span.end))
                yield span

    def _quoted(self, text: str, done: set[tuple[int, int]], whole: list[tuple[int, int]]) -> Iterator[Span]:
        """A quoted literal after a strong name and a colon or a spaced ``=`` (``_INLINE_QUOTED``), as above."""
        starts = [start for start, _ in whole]
        for m in _INLINE_QUOTED.finditer(text):
            if _within(starts, whole, m.start(1)):
                continue
            key = group(m, 1)
            kind = _key_kind(key)
            if kind is None or kind == "connection":
                continue
            before = text[m.start(1) - 1 : m.start(1)]
            if before in ("'", '"') and before == group(m, 3)[:1] and text[m.end(1) : m.end(1) + 1] != before:
                continue  # the key is in a string that the value's quote closes: `("API_KEY = ", None)`
            if group(m, 2) != ":" and text[m.end() : m.end() + 1] == ",":
                names = text[max(0, m.start(1) - 64) : m.start(1)].rstrip(" \t")
                if names.endswith(",") and names[:-1].rstrip(" \t")[-1:].isalnum():
                    continue  # `user, password = 'ada', 'lovelace'`: an unpacking, not an assignment of one value
            span = _pair_span(text, key, kind, group(m, 3), m.start(3), m.end())
            if span is not None and (span.start, span.end) not in done:
                done.add((span.start, span.end))
                yield span


def _within(starts: list[int], ranges: list[tuple[int, int]], at: int) -> bool:
    """Whether ``at`` falls inside one of ``ranges`` (in order, ``starts`` their beginnings)."""
    i = bisect.bisect_right(starts, at) - 1
    return i >= 0 and at < ranges[i][1]


def _pair_span(text: str, key: str, kind: str, raw: str, raw_start: int, raw_end: int) -> Span | None:
    """The secret in ``raw`` (a quoted or an unquoted value that begins at ``raw_start``; ``text[raw_end:]`` is what
    follows it) of a pair found in the middle of a line, or None. Read as the pattern of a line's beginning reads a
    value, but for what a line of a log holds: a name in a call is a reference (``f(token=auth_token)``, a value without
    a digit), and so is a call or an element (``password=getpass()``, ``token=tokens[0]``); a quoted value in a
    concatenation (``"API_KEY=" + GH_PAT``) is a piece of a text."""
    quoted = raw[:1] in ("'", '"')
    value = raw[1:-1] if quoted else raw
    if len(value) < (3 if kind == "password" else 6):
        return None
    if _code_like(value, key, quoted) or (_NUMBER.match(value) and kind != "password"):
        return None
    after = text[raw_end : raw_end + 2]
    if quoted:
        if value.startswith((" +", "<", "{")) or value.endswith("+ ") or after == " +" or _FORMAT.search(value):
            return None
    elif after[:1] in ("(", "[") or (
        after[:1] in (",", ")")
        and _IDENTIFIER.match(value)
        and not key.isupper()
        and not any(c.isdigit() for c in value)
    ):
        return None
    start = raw_start + (1 if quoted else 0)
    etype = "PASSWORD" if kind == "password" else "ASSIGNED_SECRET"
    return Span(start, start + len(value), etype, EntityClass.SECRET, f"assignment:{key}")


# --- PEM private keys ---------------------------------------------------------------------------

_PEM_BEGIN = re2.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----")


class PrivateKeyDetector:
    """The whole body of a PEM/OpenSSH/PGP private key block (header and footer stay visible)."""

    id = "private-key"
    mandatory = True

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if "PRIVATE KEY" not in text:
            return
        pos = 0
        while (m := _PEM_BEGIN.search(text, pos)) is not None:
            footer = f"-----END {m.group(1)}-----"
            end = text.find(footer, m.end())
            body_start = m.end()
            if end == -1:
                end = len(text)  # truncated block: hide everything after the header
            if end > body_start:
                yield Span(body_start, end, "PRIVATE_KEY", EntityClass.SECRET, "private-key:pem")
            pos = max(end, m.end())


# --- Credentials in URLs, JWTs, bearer tokens ------------------------------------------------------

_URL_CREDENTIALS = re2.compile(
    r"(?i)\b[a-z][a-z0-9+.\-]{1,30}://([^\s:/?#@'\"\\<>]{1,128}):([^\s/?#@'\"\\<>]{1,256})@[^\s/?#'\"\\<>]{1,253}"
)
_JWT = re2.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{10,}")
# One `key=value;` pair of a connection string (`Server=db;User Id=app;Password=…;`). A value can be
# quoted or in braces to hold a `;` (`Password="a;b"`, `PWD={a;b}`).
_CONNECTION_PAIR = re2.compile(
    r"([A-Za-z][A-Za-z0-9 _\-]{0,40}?)[ \t]*=[ \t]*"
    r"(\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*'|\{(?:[^}]|\}\})*\}|[^;]*)[ \t]*(?:;|$)"
)
# The keys of a connection string whose value is a credential, spaces, dashes and underscores left out.
_CONNECTION_SECRETS = frozenset(
    {
        "password", "pwd", "pass", "passwd", "secret", "clientsecret", "token", "accesstoken", "apikey",
        "accountkey", "sharedaccesskey", "sharedaccesssignature",
    }
)  # fmt: skip
_ENCLOSED = frozenset({'""', "''", "{}"})


def connection_secrets(value: str, start: int, rule: str) -> list[Span] | None:
    """The credentials of a connection string, each on its own: the password of a URL, or the values
    of ``Password=``, ``AccountKey=`` and the like in ``key=value;`` pairs. Placeholders are left
    alone (``Password={{password}};``), and so are the server, the user and the database. None when
    ``value`` is neither form: then it is a secret as a whole."""
    spans = [
        Span(start + m.start(2), start + m.end(2), "URL_PASSWORD", EntityClass.SECRET, rule)
        for m in _URL_CREDENTIALS.finditer(value)
        if not _PLACEHOLDER.match(group(m, 2))
    ]
    if "=" not in value or ";" not in value:
        return spans if "://" in value else None
    for m in _CONNECTION_PAIR.finditer(value):
        key = group(m, 1)
        kind = _key_kind(key)
        if _SEPARATORS.sub("", key).lower() not in _CONNECTION_SECRETS and kind not in ("password", "secret"):
            continue
        s, e = m.span(2)
        while e > s and value[e - 1] in " \t":
            e -= 1
        secret = value[s:e]
        if len(secret) >= 2 and secret[0] + secret[-1] in _ENCLOSED and not _PLACEHOLDER.match(secret):
            s, e, secret = s + 1, e - 1, secret[1:-1]
        if secret and not _PLACEHOLDER.match(secret):
            etype = "PASSWORD" if kind == "password" else "ASSIGNED_SECRET"
            spans.append(Span(start + s, start + e, etype, EntityClass.SECRET, rule))
    return spans


_AUTH_HEADER = re2.compile(
    r"(?i)\b(?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:bearer|basic|token|digest)[ \t]+"
    r"([A-Za-z0-9._~+/\-]{12,}=*)"
)
_BEARER = re2.compile(r"(?i)\bbearer[ \t]+([A-Za-z0-9._~+/\-]{20,}=*)")


class CredentialDetector:
    """Passwords in ``scheme://user:pass@host``, JWTs and bearer/basic credentials."""

    id = "credentials"
    mandatory = True

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if "://" in text:
            for m in _URL_CREDENTIALS.finditer(text):
                start, end = m.span(2)
                if not _PLACEHOLDER.match(text[start:end]):
                    yield Span(start, end, "URL_PASSWORD", EntityClass.SECRET, "credentials:url")
        if "eyJ" in text:
            for m in _JWT.finditer(text):
                yield Span(m.start(), m.end(), "JWT", EntityClass.SECRET, "credentials:jwt")
        lowered_has = "bearer" in text.lower() or "authorization" in text.lower()
        if lowered_has:
            for pattern, rule in ((_AUTH_HEADER, "credentials:authorization"), (_BEARER, "credentials:bearer")):
                for m in pattern.finditer(text):
                    start, end = m.span(1)
                    yield Span(start, end, "BEARER_TOKEN", EntityClass.SECRET, rule)
