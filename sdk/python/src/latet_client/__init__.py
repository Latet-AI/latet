"""A light Python client for the applications API of a Latet server: ``scan``, ``mask`` and ``unmask`` texts with an API
key's token, so that an application masks its log lines or its documents before they leave, and restores them later;
``forget`` takes values out of the key's vault.

    from latet_client import Latet

    with Latet("https://latet.example.com", token) as latet:
        masked = latet.mask(["Invoice paid by mario.rossi@acme.it"])
        original = latet.unmask(masked[0].text)

``latet_client.langchain`` (``pip install "latet-client[langchain]"``) has a document transformer and two runnables
for LangChain and LangGraph."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .client import AsyncLatet, Kind, Latet
from .errors import (
    Blocked,
    ClearTextWarning,
    ConnectionFailed,
    Forbidden,
    LatetError,
    RateLimited,
    TooLarge,
    Unauthorized,
    Unavailable,
    VaultFull,
)
from .results import Finding, ForgetResult, MaskResult, ScanResult, UnmaskResult

try:
    __version__ = version("latet-client")
except PackageNotFoundError:  # run from a copy of the source that was never installed
    __version__ = "0+unknown"

__all__ = [
    "AsyncLatet",
    "Blocked",
    "ClearTextWarning",
    "ConnectionFailed",
    "Finding",
    "Forbidden",
    "ForgetResult",
    "Kind",
    "Latet",
    "LatetError",
    "MaskResult",
    "RateLimited",
    "ScanResult",
    "TooLarge",
    "Unauthorized",
    "Unavailable",
    "UnmaskResult",
    "VaultFull",
    "__version__",
]
