"""Destinations: their names, and one for an address an agent's settings name (its company's gateway,
Azure OpenAI, Microsoft Foundry...)."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from latet.config import RESERVED_DESTINATIONS, Config, Destination

# Clouds whose resource name says more than their address: acme.openai.azure.com → azure-acme.
_CLOUDS = (
    (".openai.azure.com", "azure"),
    (".cognitiveservices.azure.com", "azure"),
    (".services.ai.azure.com", "foundry"),
)


def name_for(url: str) -> str:
    """A destination's name read off its address: `https://llm.acme.com` → `llm-acme-com`."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for suffix, cloud in _CLOUDS:
        if host.endswith(suffix) and len(host) > len(suffix):
            base = f"{cloud}-{host[: -len(suffix)]}"
            break
    else:
        base = f"{host}-{parsed.port}" if parsed.port else host
    name = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:63].strip("-") or "destination"
    return f"{name[:55]}-gateway" if name in RESERVED_DESTINATIONS else name


def add_destination(
    config: Config, url: str, agent: Literal["claude", "codex"] | None = None
) -> tuple[Config, str, bool]:
    """``config`` with a destination for ``url``: its name, and whether it is new. An address already
    there keeps its destination; a new one never takes a name in use."""
    wanted = Destination(name="new", url=url).url  # checked and written as a saved one is
    for destination in config.upstreams:
        if destination.url == wanted:
            return config, destination.name, False
    taken = {d.name for d in config.upstreams}
    base = name = name_for(wanted)
    count = 1
    while name in taken:
        count += 1
        name = f"{base[: 62 - len(str(count))]}-{count}"
    added = Destination(name=name, url=wanted, agent=agent)
    return config.model_copy(update={"upstreams": [*config.upstreams, added]}), name, True
