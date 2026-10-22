"""Whether a request that carries images, documents or audio is sent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from latet.config import Config


IMAGES_RANK = {"allow": 0, "mask": 1, "block": 2}  # how few images each setting sends


@dataclass(frozen=True, slots=True)
class ImagePolicy:
    """``allow``: images are sent, masked when they can be read; ``mask``: only what was read and
    masked is sent; ``block``: nothing Latet cannot read is sent, images included."""

    send: Literal["allow", "mask", "block"]
    because: Literal["default", "profile", "setting"]

    @property
    def blocks(self) -> bool:
        return self.send == "block"


ALLOW = ImagePolicy("allow", "default")


def image_policy(config: Config) -> ImagePolicy:
    """The policy of a configuration (the global one, or a project's effective one)."""
    from latet.config import Profile

    if config.images == "block":
        return ImagePolicy("block", "setting")
    if config.images == "mask":
        return ImagePolicy("mask", "setting")
    if config.profile == Profile.MAX:
        return ImagePolicy("block", "profile")
    return ALLOW


def refusal(policy: ImagePolicy, kinds: list[str]) -> str:
    """Why a request that carries what Latet does not read was not sent, for the agent and the dashboard."""
    what = ", ".join(sorted(set(kinds)))
    if policy.because == "profile":
        return (
            f"the max profile does not send what it cannot mask ({what}); remove it from the conversation or "
            "choose the balanced profile"
        )
    if policy.send == "mask":
        return (
            f"images: mask is set, so a request that carries what Latet cannot read and mask is not sent ({what}); "
            "install text recognition with `latet ocr install`, remove it from the conversation, or set images: allow"
        )
    return (
        f"images: block is set, so requests that carry images or documents are not sent ({what}); remove it "
        "from the conversation or set images: allow"
    )
