"""The clouds' fixed routes, Amazon Bedrock and Google Vertex, whose address follows the region; and their
model calls, known by their path on any route (a company's gateway passes them through too). And uvicorn's access
lines without their secrets (``redact_access_log``), by ``latet.logline.redacted``: what a log line may hold is that
module's, below the engine and the settings, which import nothing of the gateway for it."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import ClassVar, Literal
from urllib.parse import unquote, urlsplit

from latet.gateway.routes import Resolved, Upstream
from latet.logline import redacted

Shape = Literal["bedrock", "bedrock-count", "vertex"]

REGION_NAME = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")  # us-east-1, eu-central-2, us-gov-west-1
_LOCATION = re.compile(r"global|us|eu|[a-z]+-[a-z]+\d{1,2}")  # Vertex: us-east5, europe-west1, or multi-region
# Claude Code's calls (Bedrock's Invoke API, the model in one path segment: an ARN's slash is sent as %2F),
# and the model list it asks for at start.
_BEDROCK_CALL = re.compile(r"/model/([^/]+)/(invoke|invoke-with-response-stream|count-tokens)")
_BEDROCK_PROFILES = re.compile(r"/inference-profiles(?:/[^/]+)?")
_VERTEX_CALL = re.compile(r"/publishers/anthropic/models/([^/:]+):(rawPredict|streamRawPredict)")
_VERTEX_PATH = re.compile(r"/v1/projects/[^/]+/locations/([^/]+)" + _VERTEX_CALL.pattern)
# Google's own calls on Vertex (Gemini's, its other models', a model list): an API version, then a project and a
# location, or neither (express mode, an API key alone).
_GOOGLE_VERSION = re.compile(r"/(?:v1|v1beta1)(?:/.*)?")
_GOOGLE_LOCATED = re.compile(r"/(?:v1|v1beta1)/projects/[^/]+/locations/([^/]+)(?:/.*)?")
PROFILE_NAME = re.compile(r"[A-Za-z0-9_.+=,@-]{1,64}")  # an AWS profile's name
_KEY = re.compile(r"[0-9a-f]{32}")  # a signing route's key, or a session's id


class _AccessRedactor(logging.Filter):
    """uvicorn's access lines, which go to the console or to daemon.log, without a signing route's key."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redacted(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def redact_access_log() -> None:
    """Before the server starts (once is enough): its access lines hold each request's path."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessRedactor) for f in access.filters):
        access.addFilter(_AccessRedactor())


@dataclass(frozen=True, slots=True)
class Signing:
    """Latet signs this Bedrock request for Amazon: with an AWS profile (the route carries the key only
    this Latet makes for it), or with credentials `latet claude` handed over for its session."""

    region: str
    profile: str | None = None
    key: str | None = None
    session: str | None = None


def shape_of(method: str, path: str) -> Shape | None:
    """A Bedrock or Vertex model call, known by the end of its path (as sent) wherever it goes."""
    if method != "POST":
        return None
    path = path.split("?", 1)[0]
    if found := re.search(_BEDROCK_CALL.pattern + "$", path):
        return "bedrock-count" if found.group(2) == "count-tokens" else "bedrock"
    if re.search(_VERTEX_CALL.pattern + "$", path):
        return "vertex"
    return None


def model_of(path: str) -> str | None:
    """The model a Bedrock or Vertex call names in its path (their bodies have none)."""
    path = path.split("?", 1)[0]
    found = re.search(_BEDROCK_CALL.pattern + "$", path) or re.search(_VERTEX_CALL.pattern + "$", path)
    return unquote(found.group(1)) if found else None


def vertex_host(location: str) -> str:
    """Google's address for a Vertex location: the global one, a multi-region, or a region's own."""
    if location == "global":
        return "aiplatform.googleapis.com"
    if location in ("us", "eu"):
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


def _with(url: str, query: str) -> str:
    return f"{url}?{query}" if query else url


@dataclass(frozen=True, slots=True)
class BedrockUpstream(Upstream):
    """`/bedrock/<region>/...`: Amazon Bedrock in that region. Model calls go to its runtime and the
    model list to its control plane; anything else is a call Latet cannot read, so it has no route."""

    websocket: ClassVar[bool] = False
    name: str = "bedrock"
    prefix: str = "/bedrock"
    base_url: str = ""
    runtime: str = "https://bedrock-runtime.{region}.amazonaws.com"
    control: str = "https://bedrock.{region}.amazonaws.com"

    def resolve(self, method: str, path: str, query: str) -> Resolved | None:
        region, _, rest = path[len(self.prefix) + 1 :].partition("/")
        rest = "/" + rest
        if not REGION_NAME.fullmatch(region):
            return None
        signing = None
        if rest.startswith("/profile/"):  # /profile/<profile>/<key>/...: signed with an AWS profile
            _, _, profile, key, *more = rest.split("/", 4)
            if not more or not PROFILE_NAME.fullmatch(profile) or not _KEY.fullmatch(key):
                return None
            signing, rest = Signing(region, profile=profile, key=key), "/" + more[0]
        elif rest.startswith("/session/"):  # /session/<id>/...: signed with a session's credentials
            _, _, session, *more = rest.split("/", 3)
            if not more or not _KEY.fullmatch(session):
                return None
            signing, rest = Signing(region, session=session), "/" + more[0]
        if method == "POST" and (call := _BEDROCK_CALL.fullmatch(rest)):
            shape: Shape | None = "bedrock-count" if call.group(2) == "count-tokens" else "bedrock"
            base = self.runtime
        elif method == "GET" and _BEDROCK_PROFILES.fullmatch(rest):
            shape, base = None, self.control
        else:
            return None
        url = _with(base.format(region=region).rstrip("/") + rest, query)
        kept = f"{self.prefix}/{region}{rest}" if signing is not None else None  # no key in any record
        return Resolved(url, urlsplit(url).netloc, shape, region, signing, kept)


@dataclass(frozen=True, slots=True)
class VertexUpstream(Upstream):
    """`/vertex/...`: Google Vertex, at the address of the location the request names (Claude Code may
    use another region for each model). Claude's two calls are routed, and Google's own (Gemini's): Latet reads
    Gemini's model calls, and refuses any other body (``refuses_unread``)."""

    websocket: ClassVar[bool] = False
    name: str = "vertex"
    prefix: str = "/vertex"
    base_url: str = ""
    refuses_unread: bool = True
    base: str | None = None  # the tests' fake Google: every call to this address, path kept

    def resolve(self, method: str, path: str, query: str) -> Resolved | None:
        rest = path[len(self.prefix) :]
        if rest.startswith("/projects/"):  # a base URL without the API version: Claude Code adds none
            rest = "/v1" + rest
        if "/publishers/anthropic/" in rest:
            found = _VERTEX_PATH.fullmatch(rest)
            if method != "POST" or found is None or not _LOCATION.fullmatch(found.group(1)):
                return None
            url = _with((self.base or f"https://{vertex_host(found.group(1))}") + rest, query)
            return Resolved(url, urlsplit(url).netloc, "vertex", found.group(1))
        if not _GOOGLE_VERSION.fullmatch(rest):
            return None
        located = _GOOGLE_LOCATED.fullmatch(rest)
        location = located.group(1) if located is not None else None
        if location is not None and not _LOCATION.fullmatch(location):
            return None
        url = _with((self.base or f"https://{vertex_host(location or 'global')}") + rest, query)
        return Resolved(url, urlsplit(url).netloc, None, location)


CLOUD_UPSTREAMS: tuple[Upstream, ...] = (BedrockUpstream(), VertexUpstream())
