"""Strict model-id grammars, per provider, from the providers' documented forms.

A model id that matches its provider's grammar passes with no configuration, in a path or a body. Each grammar is
built from a closed vocabulary (model families, variant words) and numeric parts (versions, dates, revisions), so it
cannot carry free text. What is bound to an account or a project is not a model id and stays an exact owner-listed
resource: a Bedrock ARN (application inference profiles carry the account), a Vertex project. Any other value needs an
exact ``request_resources`` row, as before.
"""

from __future__ import annotations

import re

# Claude, first-party API form (models overview: aliases such as claude-opus-4-6, dated ids such as
# claude-sonnet-4-5-20250929, the 3.x order claude-3-5-sonnet-20241022, and the -latest aliases).
_CLAUDE_NAME = (
    r"claude-(?:(?:opus|sonnet|haiku|fable|mythos)(?:-[0-9]{1,2}){0,2}"
    r"|[0-9](?:-[0-9])?-(?:opus|sonnet|haiku)|mythos-preview)"
)
CLAUDE = re.compile(rf"{_CLAUDE_NAME}(?:-[0-9]{{8}}|-latest)?")
# Claude on Amazon Bedrock: the "anthropic." provider prefix; InvokeModel's versioned ids (-v1:0, -v2:0, a provisioned
# context suffix such as :28k) and the newer unversioned ids (anthropic.claude-opus-5-5); an optional system-defined
# cross-Region inference profile prefix (us., eu., apac., global.; also us-gov., jp., au.).
BEDROCK_GEOGRAPHIES = ("us", "eu", "apac", "global", "us-gov", "jp", "au")
_BEDROCK_CLAUDE = rf"anthropic\.{_CLAUDE_NAME}(?:-[0-9]{{8}})?(?:-v[0-9]{{1,2}}(?::[0-9]{{1,2}})?)?(?::[0-9]{{1,3}}k)?"
BEDROCK_MODEL = re.compile(rf"(?:(?:{'|'.join(BEDROCK_GEOGRAPHIES)})\.)?{_BEDROCK_CLAUDE}")
# A system-defined inference profile, as GET /inference-profiles/{id} names it: always geography-prefixed.
BEDROCK_SYSTEM_PROFILE = re.compile(rf"(?:{'|'.join(BEDROCK_GEOGRAPHIES)})\.{_BEDROCK_CLAUDE}")
# Claude on Vertex AI, publishers/anthropic/models/{id}: the bare id, or a dated snapshot after "@"
# (claude-sonnet-4-5@20250929; the 3.5 Sonnet v2 snapshot claude-3-5-sonnet-v2@20241022).
VERTEX_CLAUDE = re.compile(rf"{_CLAUDE_NAME}(?:-v[0-9])?(?:@[0-9]{{8}})?")
# Gemini, models/{id} on the Gemini API and publishers/google/models/{id} on Vertex AI (Gemini models page: the stable
# gemini-2.5-pro, the revisions gemini-2.0-flash-001, the previews gemini-2.5-flash-preview-09-2025, the
# -latest aliases and gemini-embedding-001; text-embedding-004 and -005 on Vertex AI).
_GEMINI_WORD = (
    r"(?:pro|flash|lite|image|tts|live|native|audio|thinking|exp|preview|latest|embedding"
    r"|[0-9]{3}|[0-9]{2}-[0-9]{4}|[0-9]{2}-[0-9]{2}|[0-9]{4})"
)
GEMINI = re.compile(
    rf"gemini-(?:[0-9](?:\.[0-9])?-)?(?:pro|flash|embedding)(?:-{_GEMINI_WORD}){{0,5}}|text-embedding-00[0-9]"
)
# OpenAI (models page): gpt-4o, gpt-4.1-mini, gpt-5.2-codex, gpt-4o-2024-08-06, the o-series (o3, o4-mini), the
# gpt-oss open weights, chatgpt-4o-latest, codex-mini-latest and the text-embedding models.
_OPENAI_WORD = (
    r"(?:mini|nano|pro|max|codex|chat|latest|turbo|preview|deep-research|search|audio|realtime|instruct|transcribe"
    r"|[0-9]{2,3}b|[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4})"
)
OPENAI = re.compile(
    rf"(?:gpt-(?:[0-9](?:\.[0-9])?o?|oss)|o[1-9]|chatgpt-[0-9]o|codex)(?:-{_OPENAI_WORD}){{0,5}}"
    r"|text-embedding-(?:3-small|3-large|ada-002)"
)


# OpenRouter's ids name the vendor (its models page: anthropic/claude-sonnet-4.5, openai/gpt-4o, google/gemini-2.5-pro,
# the openrouter/auto router) and may end in a variant (:free, :thinking...). Only the vendors Latet has a grammar for,
# each read by that grammar: an open vendor/model grammar would be an unrestricted printable string (P2-8a, ruling 6).
_OPENROUTER = re.compile(
    r"(anthropic|openai|google)/([a-z0-9.-]{1,96})(?::(?:free|nitro|floor|online|thinking|extended|beta))?"
)


def openrouter_model(value: str) -> bool:
    """OpenRouter's vendor/model ids for the vendors Latet has a grammar for, each by that grammar (Claude's versions
    written with a dot, as OpenRouter writes them), an optional variant, and openrouter/auto; any other id needs an
    exact ``request_resources`` row."""
    if len(value) > 128:
        return False
    if value == "openrouter/auto":
        return True
    found = _OPENROUTER.fullmatch(value)
    if found is None:
        return False
    vendor, model = found.groups()
    if vendor == "anthropic":
        return CLAUDE.fullmatch(re.sub(r"(?<=[0-9])\.(?=[0-9])", "-", model)) is not None
    return (OPENAI if vendor == "openai" else GEMINI).fullmatch(model) is not None


def path_model(kind: str, value: str) -> bool:
    """Whether a model id in a path passes by its provider's grammar: ``bedrock``, ``bedrock-profile``, ``vertex``
    (Claude), ``google`` (Gemini on either API), ``anthropic`` or ``openai`` (a model read)."""
    grammar = {
        "bedrock": BEDROCK_MODEL,
        "bedrock-profile": BEDROCK_SYSTEM_PROFILE,
        "vertex": VERTEX_CLAUDE,
        "google": GEMINI,
        "anthropic": CLAUDE,
        "openai": OPENAI,
    }[kind]
    return len(value) <= 128 and grammar.fullmatch(value) is not None


def body_model(protocol: str, family: str, value: str) -> bool:
    """Whether a body's ``model`` passes by grammar: Gemini's on the Gemini destination (its OpenAI-compatible route
    included), OpenRouter's vendor/model ids on OpenRouter's, Claude's for Anthropic's protocol, OpenAI's for Chat
    Completions, Responses and embeddings."""
    if len(value) > 128:
        return False
    if family == "openrouter":
        return openrouter_model(value)
    if family == "gemini" or protocol == "gemini":
        return GEMINI.fullmatch(value) is not None
    if protocol == "anthropic":
        return CLAUDE.fullmatch(value) is not None
    return OPENAI.fullmatch(value) is not None
