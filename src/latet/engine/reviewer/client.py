"""A chat client for the local reviewer's server (OpenAI-compatible: llama.cpp, Ollama, LM Studio).

The server runs on this machine, or on another machine of yours (`remote: true`, over https outside
the local network). Its API key, when it has one, is sent in a header and nowhere else: never logged,
never in an error message.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from typing import Any

import httpx
import truststore

from latet.engine.reviewer import certificate as certificates

# Certificates are checked against the operating system's store, as for the providers.
_SSL = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def key_id(key: str | None) -> str | None:
    """A short digest that tells two keys apart, without keeping the key."""
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else None


class ReviewError(Exception):
    """The review did not happen: the server could not be reached, or its answer could not be read."""


class ReviewClient:
    def __init__(
        self,
        url: str,
        model: str | None = None,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 300.0,
        pinned: str | None = None,
        certificate: str | None = None,
    ) -> None:
        """``pinned``: the fingerprint of the certificate you trusted, and ``certificate`` that certificate
        (None when it went missing: nothing is accepted then). Without ``pinned``, the operating
        system's authorities, as for the providers."""
        self.url = url.rstrip("/")
        self.model = model
        self.key_id = key_id(api_key)
        self._pinned, self._certificate = pinned, certificate
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=3.0),
            transport=transport,
            verify=certificates.context(certificate) if pinned else _SSL,
            trust_env=False,  # no proxy from the environment: the server is yours, here or on your network
        )

    async def models(self) -> list[str]:
        """The models the server offers."""
        try:
            response = await self._http.get(f"{self.url}/models")
            response.raise_for_status()
            return [str(m["id"]) for m in response.json().get("data") or [] if isinstance(m, dict) and "id" in m]
        except httpx.HTTPStatusError as exc:
            raise ReviewError(f"the server answered HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            raise ReviewError(self._unreachable(exc)) from None
        except (ValueError, KeyError, TypeError):
            raise ReviewError("the server's list of models could not be read") from None

    async def props(self) -> dict[str, Any] | None:
        """llama.cpp's own settings (``/props``): the context of each slot and how many slots. Other
        servers have none."""
        try:
            response = await self._http.get(f"{self.url.removesuffix('/v1')}/props")
            if response.status_code != 200:
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def model_id(self) -> str:
        if not self.model:
            listed = await self.models()
            if not listed:
                raise ReviewError("the server lists no model")
            self.model = listed[0]
        return self.model

    async def ask(self, system: str, text: str, schema: dict[str, Any], image: str | None = None) -> str:
        """The model's answer to ``text``, constrained to ``schema``, without its reasoning. ``image``: a
        data URL shown to it with the text (a model that sees images).

        The answer is streamed: cancelling the call closes the connection, and the server stops
        working on it (a review in the background makes room for one a request waits for).
        """
        user: str | list[dict[str, Any]] = text
        if image is not None:
            user = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image}}]
        body = {
            "model": await self.model_id(),
            "temperature": 0,
            "max_tokens": 1536,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "review", "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False},
            "cache_prompt": True,  # llama.cpp: Latet's instructions come first, the same every time
        }
        try:
            async with self._http.stream("POST", f"{self.url}/chat/completions", json=body) as response:
                if response.status_code >= 400:
                    raise ReviewError(f"the server answered HTTP {response.status_code}")
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    data = json.loads(await response.aread())  # a server that does not stream
                    return str(data["choices"][0]["message"].get("content") or "")
                parts: list[str] = []
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    choices = json.loads(payload).get("choices") or [{}]
                    content = (choices[0].get("delta") or {}).get("content")
                    if content:
                        parts.append(str(content))
                return "".join(parts)
        except httpx.HTTPError as exc:
            raise ReviewError(self._unreachable(exc)) from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ReviewError("the server's answer could not be read") from None

    def _unreachable(self, exc: httpx.HTTPError) -> str:
        refused = certificates.why_refused(exc, self._pinned, self._certificate)
        return refused or f"the server could not be reached ({type(exc).__name__})"

    async def aclose(self) -> None:
        await self._http.aclose()
