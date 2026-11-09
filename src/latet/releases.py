"""Latet's releases on GitHub: what the installers and `latet upgrade` install (each goes to PyPI too).

A release carries the wheel CI built, with the dashboard in it; the repository's source has no dashboard
until someone builds it, so it is never what gets installed. Latet comes with local name detection and the
text in images (the `ner` and `ocr` extras); their models are downloaded only when you ask. An upgrade keeps the
other extras the installed Latet has (a server's `server`): uv uninstalls those a requirement leaves out.
"""

from __future__ import annotations

import re
import ssl
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

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
EXTRAS = ("ner", "ocr")  # local name detection and the text in images, which every Latet installed gets
# uv's record of the requirement it installed a tool from, extras included, in the tool's folder: its environment.
RECEIPT = "uv-receipt.toml"
_EXTRA = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")  # an extra's name, as PEP 508 has it
_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")  # a package's name, where a requirement starts
# A requirement as an older uv wrote it in a receipt (`latet[ner,ocr] @ <wheel>`): the name, the extras in brackets,
# then what is no part of either (an address, versions, a marker).
_WRITTEN = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\[\]]*)\])?\s*(?:[@;<>=!~(].*)?", re.DOTALL)
_UV_PYTHON = re.compile(r"(?:cpython|pypy|graalpy)-\d", re.IGNORECASE)  # the folder of a Python uv installed
_ONLY_EXTRA = re.compile(r"\s*extra\s*==\s*(['\"])([^'\"]+)\1\s*")  # a Requires-Dist's marker: an extra, alone
# The Pythons Latet is tested on, as the installers ask: uv takes the newest Python it finds or can download, and on a
# newer one Latet's dependencies may have no wheel yet (google-re2 then has to be built, and cannot be).
PYTHONS = ">=3.12,<3.15"


class ReleaseError(Exception):
    """No release to install, and why."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    wheel: str  # the address of its wheel

    def requirement(self, extras: bool = True, keep: Iterable[str] = ()) -> str:
        """What uv installs: the wheel, with local name detection and the text in images or without them, and with the
        other extras in ``keep``, those of the Latet installed (``installed_extras``): uv installs exactly the extras a
        requirement names, and uninstalls the others."""
        return named(set(keep).difference(EXTRAS).union(EXTRAS if extras else ()), self.wheel)


def named(extras: Iterable[str], wheel: str) -> str:
    """Latet from ``wheel`` with exactly ``extras``, as uv takes it; ValueError for what is no name of an extra."""
    extras = sorted(set(extras))
    if not all(_EXTRA.fullmatch(extra) for extra in extras):
        raise ValueError(f"not the names of extras: {extras}")
    return f"latet[{','.join(extras)}] @ {wheel}" if extras else f"latet @ {wheel}"


@dataclass(frozen=True, slots=True)
class Folders:
    """Where uv installed the Latet that runs, as its variables name them: its tools (UV_TOOL_DIR), its commands
    (UV_TOOL_BIN_DIR) and its Python (UV_PYTHON_INSTALL_DIR). None where it cannot be told."""

    tools: Path
    commands: Path | None
    pythons: Path | None


def installed_extras() -> frozenset[str]:
    """The extras the Latet that runs was installed with, which `latet upgrade` names again (a server's `server`, with
    psycopg and redis): from uv's receipt in the tool's folder, the record of the requirement uv installed and replaces;
    where there is none or it cannot be read (a Latet not installed by uv, a receipt of a form not known here), every
    extra whose packages are all installed here."""
    found = _from_receipt(_tool_folder() / RECEIPT)
    return _present() if found is None else found


def tool_folders() -> Folders | None:
    """The folders uv installed the Latet that runs in; None where uv did not install it (no receipt in its folder):
    the tools' is the one its folder is in, the commands' the one its receipt says `latet` was put in, the Python's
    the one uv keeps the Python it runs on in, when uv installed that Python."""
    folder = _tool_folder()
    receipt = folder / RECEIPT
    if not receipt.is_file():
        return None
    python = _python_folder()
    pythons = python.parent if _UV_PYTHON.match(python.name) else None
    return Folders(folder.parent, _commands_folder(receipt), pythons)


def _tool_folder() -> Path:
    """The folder of the Latet that runs: a tool's environment, where uv keeps its receipt."""
    return Path(sys.prefix)


def _python_folder() -> Path:
    """The folder of the Python the Latet that runs is on (``cpython-3.13.9-linux-x86_64-gnu`` when uv installed it)."""
    return Path(sys.base_prefix)


def _commands_folder(receipt: Path) -> Path | None:
    """The folder uv's receipt says it put the `latet` command in; None where it says none."""
    try:
        found = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tool = found.get("tool")
    entrypoints = tool.get("entrypoints") if isinstance(tool, dict) else None
    for entry in entrypoints if isinstance(entrypoints, list) else []:
        if isinstance(entry, dict) and entry.get("name") == "latet" and isinstance(entry.get("install-path"), str):
            return Path(entry["install-path"]).parent
    return None


def _normal(name: str) -> str:
    """A package's or an extra's name as uv compares them (`Latet`, `latet`; `a_b`, `a-b`)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _from_receipt(path: Path) -> frozenset[str] | None:
    """The extras uv's receipt ``path`` records for Latet; None where it is missing, cannot be read, names no Latet, or
    gives an extra that is no name (never put in a requirement)."""
    try:
        receipt = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # missing, a folder, not UTF-8, not TOML
        return None
    tool = receipt.get("tool")
    requirements = tool.get("requirements") if isinstance(tool, dict) else None
    found: set[str] | None = None  # the extras of every entry for Latet: it can be named twice (`--with "latet[x]"`)
    for entry in requirements if isinstance(requirements, list) else []:
        if isinstance(entry, dict):  # the requirement's parts, as uv writes them
            name, extras = entry.get("name"), entry.get("extras", [])
        elif isinstance(entry, str):  # the requirement as it was given, as an older uv wrote it
            start = _NAME.match(entry)
            if start is None or _normal(start.group(1)) != "latet":
                continue
            written = _WRITTEN.fullmatch(entry)
            if written is None:
                return None
            name, extras = written.group(1), [part.strip() for part in (written.group(2) or "").split(",")]
            extras = extras if extras != [""] else []
        else:
            continue
        if not isinstance(name, str) or _normal(name) != "latet":
            continue  # a package installed beside it (`--with`)
        if not isinstance(extras, list) or not all(isinstance(e, str) and _EXTRA.fullmatch(e) for e in extras):
            return None
        found = (found or set()) | {_normal(extra) for extra in extras}
    # An extra the new release no longer has stays named: uv warns that the release has no such extra, and goes on.
    # It is not checked against this version's extras, which a newer release (`--to`) can outgrow.
    return None if found is None else frozenset(found)


def _present() -> frozenset[str]:
    """The extras whose packages are all installed here, as Latet's own metadata lists them: each Requires-Dist with
    the extra it comes with. One named under a further condition too (a platform) is not looked for, as whether it
    applies here cannot be told without the `packaging` library."""
    wanted: dict[str, set[str]] = {}
    for line in _requires() or []:
        requirement, _, marker = line.partition(";")
        extra, name = _ONLY_EXTRA.fullmatch(marker), _NAME.match(requirement)
        if extra is not None and name is not None and _EXTRA.fullmatch(extra.group(2)):
            wanted.setdefault(_normal(extra.group(2)), set()).add(name.group(1))
    return frozenset(extra for extra, names in wanted.items() if all(_installed(name) for name in names))


def _requires() -> list[str] | None:
    """Latet's own Requires-Dist; None where it has no metadata."""
    try:
        return metadata.requires("latet")
    except metadata.PackageNotFoundError:
        return None


def _installed(name: str) -> bool:
    """Whether the package ``name`` is installed here."""
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    return True


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
