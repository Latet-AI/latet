"""Latet's releases on GitHub: what the installers and `latet upgrade` install (each goes to PyPI too).

A release carries the wheel CI built, with the dashboard in it; the repository's source has no dashboard
until someone builds it, so it is never what gets installed. Latet comes with local name detection and the
text in images (the `ner` and `ocr` extras); their models are downloaded only when you ask.
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass

import httpx
import truststore

REPOSITORY = "Latet-AI/latet"
LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
TAGGED = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/"  # and the tag: `latet upgrade --to`
PAGE = f"https://github.com/{REPOSITORY}/releases"
# The page of the latest release: GitHub redirects it to the release's tag. Not the API, so not its limit of 60
# requests an hour from one address.
LATEST_PAGE = f"{PAGE}/latest"
_DOWNLOADS = f"{PAGE}/download/"  # a wheel is taken from Latet's own releases only
_TAG = re.compile(rf"{re.escape(PAGE)}/tag/(v[0-9][0-9A-Za-z.+-]*)")
_VERSION = re.compile(r"v?([0-9][0-9A-Za-z.+-]*)")  # a version as `latet upgrade --to` takes it: 0.9.9, or v0.9.9
EXTRAS = "ner,ocr"
# The Pythons Latet is tested on, as the installers ask: uv takes the newest Python it finds or can download, and on a
# newer one Latet's dependencies may have no wheel yet (google-re2 then has to be built, and cannot be).
PYTHONS = ">=3.12,<3.14"


class ReleaseError(Exception):
    """No release to install, and why."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    wheel: str  # the address of its wheel

    def requirement(self, extras: bool = True) -> str:
        """What uv installs: the wheel, with local name detection and the text in images or without them."""
        return f"latet[{EXTRAS}] @ {self.wheel}" if extras else f"latet @ {self.wheel}"


def latest(client: httpx.Client | None = None) -> Release:
    """The newest release of Latet on GitHub: from GitHub's API, or, where the API does not answer, from the
    page of the latest release, which names its tag (its wheel is named after it, as the release workflow checks)."""
    http = client or httpx.Client(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=30.0)
    try:
        return _latest(http)
    finally:
        if client is None:
            http.close()


def _latest(http: httpx.Client) -> Release:
    try:
        response = http.get(LATEST, headers={"accept": "application/vnd.github+json"}, follow_redirects=True)
    except httpx.HTTPError as exc:
        why = f"GitHub could not be reached ({exc})"
    else:
        if response.status_code == 200:
            return _from_api(response)
        if response.status_code == 404:
            raise ReleaseError(f"there is no release of Latet yet ({PAGE})")
        why = f"GitHub's API answered {response.status_code}"
        if response.status_code in (403, 429):
            why += " (it takes 60 requests an hour from one address)"
    release = _from_page(http)
    if release is None:
        raise ReleaseError(why)
    return release


_RELEASE = re.compile(r"v?(\d+(?:\.\d+)*)(.*)")


def older(version: str, than: str) -> bool:
    """Whether release ``version`` (`0.9.9`, `v0.9.9`) comes before ``than``: by its numbers, a pre-release (`1.0.0rc1`,
    `1.0.0-rc1`) before its release. One that is no version counts as older: what it would install is not known."""
    found, other = _RELEASE.fullmatch(version.strip()), _RELEASE.fullmatch(than.strip())
    if found is None or other is None:
        return True
    mine, theirs = [int(n) for n in found.group(1).split(".")], [int(n) for n in other.group(1).split(".")]
    width = max(len(mine), len(theirs))
    mine, theirs = mine + [0] * (width - len(mine)), theirs + [0] * (width - len(theirs))
    if mine != theirs:
        return mine < theirs
    return bool(found.group(2)) and not other.group(2)


def tagged(version: str, client: httpx.Client | None = None) -> Release:
    """The release of Latet tagged ``v<version>`` on GitHub (`latet upgrade --to`), looked up as ``latest`` looks up
    the newest: from GitHub's API, or, where the API does not answer, from the release's page, which is there when the
    release is (its wheel is named after its tag). Raises ValueError for what is no version."""
    found = _VERSION.fullmatch(version.strip())
    if found is None:
        raise ValueError(f"{version!r} is not a version of Latet, such as 0.9.9")
    tag = f"v{found.group(1)}"
    http = client or httpx.Client(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=30.0)
    try:
        return _tagged(http, tag)
    finally:
        if client is None:
            http.close()


def _tagged(http: httpx.Client, tag: str) -> Release:
    try:
        response = http.get(TAGGED + tag, headers={"accept": "application/vnd.github+json"}, follow_redirects=True)
    except httpx.HTTPError as exc:
        why = f"GitHub could not be reached ({exc})"
    else:
        if response.status_code == 200:
            return _from_api(response, f"the release {tag}")
        if response.status_code == 404:
            raise ReleaseError(f"there is no release {tag} of Latet ({PAGE})")
        why = f"GitHub's API answered {response.status_code}"
        if response.status_code in (403, 429):
            why += " (it takes 60 requests an hour from one address)"
    try:
        page = http.get(f"{PAGE}/tag/{tag}", follow_redirects=False)
    except httpx.HTTPError:
        raise ReleaseError(why) from None
    if page.status_code != 200:
        raise ReleaseError(why)
    version = tag.removeprefix("v")
    return Release(version, f"{_DOWNLOADS}{tag}/latet-{version}-py3-none-any.whl")


def _from_api(response: httpx.Response, which: str = "the latest release") -> Release:
    try:
        found = response.json()
    except ValueError:
        found = None
    if not isinstance(found, dict):
        raise ReleaseError("GitHub's answer could not be read")
    wheels = [
        str(asset.get("browser_download_url"))
        for asset in found.get("assets") or []
        if isinstance(asset, dict) and str(asset.get("browser_download_url")).startswith(_DOWNLOADS)
        and str(asset.get("name", "")).endswith(".whl")
    ]  # fmt: skip
    if not wheels:
        raise ReleaseError(f"{which} has no wheel of Latet ({PAGE})")
    # A wheel's file name holds its version (latet-1.2.0-py3-none-any.whl): for a release with no tag name.
    named = wheels[0].rsplit("/", 1)[-1].split("-")
    version = str(found.get("tag_name") or "").removeprefix("v") or (named[1] if len(named) >= 5 else "")
    return Release(version, wheels[0])


def _from_page(http: httpx.Client) -> Release | None:
    """The latest release from its page, which GitHub redirects to the release's tag; None when it names none."""
    try:
        response = http.get(LATEST_PAGE, follow_redirects=False)
    except httpx.HTTPError:
        return None
    found = _TAG.fullmatch(response.headers.get("location", ""))
    if not response.is_redirect or found is None:
        return None
    tag = found.group(1)
    version = tag.removeprefix("v")
    return Release(version, f"{_DOWNLOADS}{tag}/latet-{version}-py3-none-any.whl")
