"""A request member no profile declares: why the walk refuses it, or what it is read as (the new rule after D6's string
case, ``notes/unknown-members-design.md`` §1).

Refused, as an unknown member is refused today, when any part of it is something the provider acts on or Latet cannot
read: a URL or URI of any scheme, a run long enough to be a blob, an id with the prefix of a stored resource, a key
naming a resource, a destination or a model, a number that is not finite, a value that is not JSON data; inside a
provider's item, anything but short tokens; a key of more than ``KEY_CHARS`` characters. Otherwise read whole: every
key and every string and number leaf as a text, a leaf under a key that names a password, a secret or a connection also
with its key (``key: value``), and the strings of an array joined, so that a secret split from its name or across
chunks is seen; and each leaf under a key as its field (``fields``), so that your field rules find it by its key or its
path. The words a refusal is given here are fixed categories, never what the request named: a member's name is the
client's text (R-79)."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from typing import Any

from latet.engine.detectors.structural import _key_kind, _words

# The categories a member is refused by.
DESTINATION = "destination"
BLOB = "blob"
RESOURCE = "resource"
NUMBER = "number"
SHAPE = "shape"
PROVIDER = "provider item"
LENGTH = "length"  # a key longer than ``KEY_CHARS``

# A key of the member, or one inside it, of more characters than this is refused: no field the agents add has a name
# near it, and every key is read for the words it is made of (``_names``) on each of the walk's passes.
KEY_CHARS = 256
# What the new rule reads of one request, at most: so many distinct texts (its members' keys and values, what it makes
# of them: a key with its value, an array's strings joined, a value as its field) and so many bytes of them in UTF-8,
# in all of its new members together. Over either, the request is refused as before the rule. The members the agents
# have added (Claude Code's output_config and context_management, a mode in a message's content block) are read as a
# few dozen texts of a few hundred bytes; one in every content block of a conversation of a thousand blocks, a different
# value in each, as some two thousand (the same value in each, as a few). Each text is one more for the final check to
# read, the local name model included, and one more to record as sent in its conversation (on a Latet server, in
# Valkey).
TEXTS = 4096
BYTES = 1 << 18

# A URI: a scheme of RFC 3986's grammar (section 3.1, ``ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )``) before "//",
# found by its last character, which keeps the search linear; a reference relative to the scheme (section 4.2,
# "//host/path", where a word or a path does not run into the "//"); or a scheme that names a resource without "//" (and
# those with it, written without), what it names right after its colon (prose's "data: 5" names nothing). gs:// and
# s3:// (Google's and Amazon's storage) are among them.
_URI = re.compile(r"[A-Za-z0-9+.-]://|(?<![A-Za-z0-9+.:/_-])//[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?(?:[/?#\s]|$)")
_SCHEME = re.compile(
    r"(?<![A-Za-z0-9+._-])(?:data|mailto|file|urn|tel|sms|javascript|vbscript|blob|about|magnet|news|cid|mid|ftp|"
    r"https?|wss?|git|ssh|s3|gs):(?=\S)",
    re.IGNORECASE,
)
# Content in an encoding: a run of base64's characters (either alphabet) this long that mixes digits, capitals and small
# letters as an encoding does (a long snake_case name, an MCP tool's, does not), or a run of hexadecimal digits this
# long. A commit's 40 hex digits and a UUID's runs pass.
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{64,}")
_MIXED = (re.compile(r"[0-9]"), re.compile(r"[A-Z]"), re.compile(r"[a-z]"))
_HEX = re.compile(r"[0-9A-Fa-f]{48}")
# The ids of stored resources a provider looks up: Anthropic's files, containers, message batches and skills; OpenAI's
# files, uploads, conversations, responses, containers and their files, vector stores and their batches, assistants,
# stored prompts and fine-tuning (jobs and models); Amazon's resource names (Bedrock's models, inference profiles,
# guardrails); Google's files, caches, corpora, tuned models, file search stores, models and Vertex's resource names.
# Each where a token starts, never inside a path (src/models/user.py, ~/projects/app). A prefix that begins words too
# (file_read, a file-based cache, vs_code) names an id only before the 20 letters and digits or more its ids have;
# prefixes that are common words (run_, thread_, batch_) are left out.
_RESOURCE_ID = re.compile(
    r"(?<![A-Za-z0-9_/-])(?:"
    r"(?:conv_|resp_|cntr_|cfile_|vsfb_|asst_|pmpt_|msgbatch_|ftjob-)[A-Za-z0-9]"
    r"|(?:file-|file_|upload_|skill_|container_|vs_)[A-Za-z0-9]{20}"
    r"|ft:(?=\S)|arn:[A-Za-z0-9-]+:|files/|cachedContents/|corpora/|tunedModels/|fileSearchStores/|models/"
    r"|projects/[^/\s]+/locations/|publishers/[^/\s]+/models/)"
)
# A key's words that name what the declared rules guard: a resource, a destination, a model, media Latet does not read.
_NAMED = frozenset(
    {
        "resource", "url", "uri", "file", "server", "mcp", "container", "conversation", "model", "cache", "image",
        "document", "audio", "attachment", "media", "source", "endpoint", "webhook", "callback", "redirect", "host",
        "hostname", "domain", "upload", "video", "vector", "bucket", "corpus", "corpora", "dataset", "blob", "pdf",
        "screenshot", "photo",
    }
)  # fmt: skip
_APART = re.compile(r"[^A-Za-z0-9]+")
# URLs, URIs: one word, its plural. A run of capitals is tried where it starts alone (the look behind): tried at each of
# its letters, a run with no plural after it cost the square of its length (a key of 32,000 capitals, seconds).
_ACRONYMS = re.compile(r"(?<![A-Z])([A-Z]{2,})s(?![a-z])")
# Inside a provider's item: each key and string leaf a token of this form, and so many leaves and bytes at most in all
# of the item's unknown members (``measure``).
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
PROVIDER_LEAVES = 32
PROVIDER_BYTES = 2048
# A leaf read with its key: the value's first characters and the key's last (the words that say its kind).
_PAIRED = 256
# An array's strings read joined: so many characters of them at most.
_JOINED = 4096


def refusal(key: str, value: Any, *, provider: bool) -> str | None:
    """Why the member ``key`` holding ``value`` is refused: a fixed category, never the key; None for one read whole.
    ``provider``: it sits inside a provider's item, where only short tokens pass."""
    leaves = size = 0
    pending: list[tuple[object, Any]] = [(key, value)]
    while pending:
        name, item = pending.pop()
        found = None
        if name is not None:
            if not isinstance(name, str):
                return SHAPE
            if len(name) > KEY_CHARS:  # before any reading of it
                return LENGTH
            found = _text(name) or (RESOURCE if _names(name) else None)
            if found is None and provider and not _TOKEN.fullmatch(name):
                found = PROVIDER
            if found is not None:
                return found
            size += len(name)
        if isinstance(item, dict):
            pending.extend(item.items())
        elif isinstance(item, list):
            pending.extend((None, child) for child in item)
        elif isinstance(item, str):
            leaves += 1
            found = _text(item)
            if found is None and provider and not _TOKEN.fullmatch(item):
                found = PROVIDER
            size += len(item)
        elif type(item) in (int, float):
            leaves += 1
            text = _number(item)
            found = NUMBER if text is None else BLOB if _blob(text) else None
            size += len(text or "")
        elif item is None or type(item) is bool:
            leaves += 1
        else:
            found = SHAPE
        if found is None and provider and (leaves > PROVIDER_LEAVES or size > PROVIDER_BYTES):
            found = PROVIDER
        if found is not None:
            return found
    return None


def measure(key: str, value: Any) -> tuple[int, int]:
    """The leaves of a member and its bytes as ``refusal`` counts them (its keys, strings and numbers): what a
    provider's item holds of it."""
    leaves = size = 0
    pending: list[tuple[str | None, Any]] = [(key, value)]
    while pending:
        name, item = pending.pop()
        size += len(name) if name is not None else 0
        if isinstance(item, dict):
            pending.extend(item.items())
        elif isinstance(item, list):
            pending.extend((None, child) for child in item)
        else:
            leaves += 1
            text = item if isinstance(item, str) else _number(item) if type(item) in (int, float) else None
            size += len(text) if text is not None else 0
    return leaves, size


def texts(key: str, value: Any) -> list[str]:
    """What a member is read as, in reading order: its key, every key, string leaf and finite number in it, each leaf
    under a key the fields detector takes for a password's, a secret's or a connection's also as ``key: value``, and
    after an array of two strings or more its strings joined."""
    return [text for text, _ in read(key, value)]


_VALUE, _KEY, _JOIN = 0, 1, 2


def read(key: str, value: Any) -> Iterator[tuple[str, bool]]:
    """``texts``, one at a time, each with whether it is made here (a ``key: value`` or a join) rather than a key or a
    leaf of the body, so that the walk charges what it makes before the next is made."""
    yield key, False
    # A value to read (with the nearest key above it), a key to give, or an array whose strings are given joined once
    # its items are read.
    pending: list[tuple[int, str, Any]] = [(_VALUE, key, value)]
    while pending:
        what, near, item = pending.pop()
        if what == _KEY:
            yield near, False
        elif what == _JOIN:
            strings = joined(item)
            if strings is not None:
                yield strings, True
        elif isinstance(item, dict):
            for name, child in reversed(item.items()):
                pending.append((_VALUE, name, child))
                pending.append((_KEY, name, None))
        elif isinstance(item, list):
            pending.append((_JOIN, near, item))
            pending.extend((_VALUE, near, child) for child in reversed(item))
        else:
            text = item if isinstance(item, str) else _number(item) if type(item) in (int, float) else None
            if text is None:
                continue
            yield text, False
            if _key_kind(near) is not None:
                yield f"{near[-_PAIRED:]}: {text[:_PAIRED]}", True


def fields(key: str, value: Any) -> Iterator[str]:
    """Each string and finite number leaf of a member as its field, in reading order: one JSON object of one member,
    ``{"<a field>": <its value>}`` (``field``), for each field it stands in, as a field rule names one: its key, its
    path inside the member, and its path from the member's own key (``x_ctx.items[].sku``: ``sku``, ``items[].sku``,
    ``x_ctx.items[].sku``), each once. Its first ``_PAIRED`` characters, a field's last. The final check reads these
    with every detector but the local name model, and reads the value by its field as masking reads a JSON document's
    (``Verifier.verify``'s ``new_member_fields``): a value of your production data, which a field rule finds by its key
    or its path, stops the request there too. An array's items stand in the field of the key that holds the array
    (``tags``, ``tags[]``); a boolean or a null in none."""
    # A value to read, the nearest key above it, and its path inside the member: its segments (a key, or "[]").
    pending: list[tuple[str, tuple[str, ...], Any]] = [(key, (), value)]
    while pending:
        near, inside, item = pending.pop()
        if isinstance(item, dict):
            pending.extend((name, (*inside, name), child) for name, child in reversed(item.items()))
        elif isinstance(item, list):
            pending.extend((near, (*inside, "[]"), child) for child in reversed(item))
        elif isinstance(item, str) or (type(item) in (int, float) and _number(item) is not None):
            leaf = item[:_PAIRED] if isinstance(item, str) else item
            for path in dict.fromkeys((near, _path(inside), _path((key, *inside)))):
                if path:
                    yield field(path, leaf)


def field(path: str, value: str | int | float) -> str:
    """A value as the field ``path`` holds it (``fields``): ``{"<path>": <value>}``, the path's last ``_PAIRED``
    characters, as JSON writes them, any character as itself."""
    return json.dumps({path[-_PAIRED:]: value}, ensure_ascii=False)


def _path(segments: tuple[str, ...]) -> str:
    """A field's path as masking names it, a field rule too: keys apart by a dot, an array's items by ``[]`` after the
    key that holds them (``items[].sku``). A path that starts in an array (the member itself a list) starts at its first
    key (``[].id`` is ``id``); one in no key is none."""
    path = ""
    for segment in segments:
        path += segment if segment == "[]" or not path else "." + segment
    while path.startswith("[]"):
        path = path[2:].removeprefix(".")
    return path


def _text(text: str) -> str | None:
    if _RESOURCE_ID.search(text):  # first: an ARN holds a service's name after a colon (arn:aws:s3:...)
        return RESOURCE
    if _URI.search(text) or _SCHEME.search(text):
        return DESTINATION
    if _blob(text):
        return BLOB
    return None


def _blob(text: str) -> bool:
    return _HEX.search(text) is not None or any(
        all(kind.search(run.group()) for kind in _MIXED) for run in _BASE64.finditer(text)
    )


def _names(key: str) -> bool:
    """Whether a word of ``key`` (split at anything but a letter or a digit and at changes of case; a plural and
    trailing digits read as the word) names a resource, a destination or a model."""
    for word in _words(_APART.sub("_", _ACRONYMS.sub(lambda found: found.group(1) + "S", key))):
        word = word.rstrip("0123456789")
        if word in _NAMED or (word.endswith("s") and word[:-1] in _NAMED):
            return True
    return False


def _number(value: int | float) -> str | None:
    """A number's digits as a text; None for one not finite, or an integer too long to write."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return str(value)
    except ValueError:  # past the interpreter's limit on an integer's digits
        return None


def joined(items: list[Any]) -> str | None:
    """The strings of an array joined, to ``_JOINED`` characters; None for fewer than two."""
    parts, length, count = [], 0, 0
    for item in items:
        if not isinstance(item, str):
            continue
        count += 1
        if length < _JOINED:
            parts.append(item[: _JOINED - length])
            length += len(parts[-1])
    return "".join(parts) if count >= 2 else None
