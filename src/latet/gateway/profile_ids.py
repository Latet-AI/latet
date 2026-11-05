"""Every request profile id the built-in profiles produce (``request_profiles``), one closed set: what a request
exception may name (``config.RequestException.profile``) and what the history's summary keeps of the profile a request
was admitted by. A WebSocket handshake's id is one of these with ``WS_SUFFIX`` (the history's alone: no exception
applies to a WebSocket in 0.11.0). A profile added to ``request_profiles`` is added here, or its exceptions are refused
and its history shows no profile; ``tests/unit/test_profile_ids.py`` holds the set against the observed calls."""

from __future__ import annotations

WS_SUFFIX = ".ws-envelope-1"

_GEMINI_OPERATIONS = (
    "models",
    "model",
    "generateContent",
    "streamGenerateContent",
    "countTokens",
    "embedContent",
    "batchEmbedContents",
    "cache-create",
    "cache-patch",
    "openai.completions",
    "openai.embeddings",
)

PROFILE_IDS: frozenset[str] = frozenset(
    {
        *(f"anthropic.{operation}.1" for operation in ("messages", "count_tokens", "models", "model")),
        *(
            f"openai.{operation}.1"
            for operation in ("chat", "completions", "responses", "embeddings", "models", "model")
        ),
        *(
            f"bedrock.{operation}.1"
            for operation in (
                "invoke",
                "invoke-with-response-stream",
                "count-tokens",
                "inference-profiles",
                "inference-profile",
            )
        ),
        "vertex.anthropic.rawPredict.1",
        "vertex.anthropic.streamRawPredict.1",
        "chatgpt.codex.models.1",
        "chatgpt.codex.responses.1",
        *(f"azure-openai.{operation}.1" for operation in ("chat", "embeddings", "completions", "responses")),
        *(f"foundry.{operation}.1" for operation in ("chat", "embeddings")),
        *(f"company.bedrock.{operation}.1" for operation in ("invoke", "invoke-with-response-stream", "count-tokens")),
        "company.vertex.rawPredict.1",
        "company.vertex.streamRawPredict.1",
        *(
            f"gemini.{version}.{operation}.1"
            for version in ("v1", "v1beta", "v1beta1")
            for operation in _GEMINI_OPERATIONS
        ),
    }
)


def admitted(profile: object, *, websocket: bool = False) -> bool:
    """Whether ``profile`` is an id a built-in profile produces; ``websocket``: a handshake's form of one too."""
    if type(profile) is not str:
        return False
    if websocket and profile.endswith(WS_SUFFIX):
        profile = profile[: -len(WS_SUFFIX)]
    return profile in PROFILE_IDS
