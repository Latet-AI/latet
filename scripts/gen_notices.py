"""Generate THIRD_PARTY_NOTICES.md from the project environment.

Usage (from the repository root):

    uv run python scripts/gen_notices.py           # (re)write THIRD_PARTY_NOTICES.md
    uv run python scripts/gen_notices.py --check   # exit 1 if the file is out of date (CI)

Sources, in output order:

1. Vendored and ported code: hand-maintained in ``VENDORED`` below, with the upstream
   license texts stored in ``scripts/notices/``.
2. Runtime-image supplements: verified inputs listed in ``scripts/notices/runtime-image.json``
   with original license texts stored beside that manifest, independent of the generator's OS.
3. Python distributions installed in the project environment (``importlib.metadata``),
   excluding Latet's own packages (``OWN``). ``uv.lock`` decides which installed distributions belong to the
   project and whether each one is a runtime or a development dependency, so the output does
   not depend on stray packages in the environment.
4. npm packages declared in ``dashboard/package.json`` and ``docs/package.json``, read from
   their ``node_modules`` (run ``npm ci`` in those directories first). Projects without a
   ``package.json`` are skipped.

Packages that ``uv.lock`` installs only on some operating systems (for example ``colorama`` on
Windows) are listed by name, version and marker only, so the output is identical whichever OS
generates it. Every other locked package must be installed: run ``uv sync --all-extras`` (the
default ``uv run`` sync includes the dev group) before generating or checking.

Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.metadata as md
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.md"
NOTICES_DIR = Path(__file__).resolve().parent / "notices"
SELF = "latet"
# The repository's own packages: Latet itself and its Python client in sdk/python (MIT license, its own LICENSE). They
# are Latet's, not third-party software, so they are not listed; what they need is reached through them as if the
# package that asks for them asked for it too.
OWN = frozenset({SELF, "latet-client"})

# Hand-maintained: third-party code copied or ported into this repository.
VENDORED: tuple[dict[str, str], ...] = (
    {
        "name": "gitleaks",
        "url": "https://github.com/gitleaks/gitleaks",
        "license": "MIT",
        "usage": (
            "The default secret-detection ruleset (`config/gitleaks.toml` in the gitleaks repository) "
            "is vendored and converted for use with RE2."
        ),
        "license_file": "gitleaks-LICENSE.txt",
    },
    {
        "name": "Presidio",
        "url": "https://github.com/data-privacy-stack/presidio",
        "license": "MIT",
        "usage": (
            "Several PII recognizers (patterns, context words and checksum validation) are ported from "
            "presidio-analyzer. Presidio was originally developed by Microsoft; releases published before "
            "the project moved to the Data Privacy Stack organization carry the notice "
            '"Copyright (c) Microsoft Corporation. All rights reserved."'
        ),
        "license_file": "presidio-LICENSE.txt",
    },
    {
        "name": "IBM Plex Sans",
        "url": "https://github.com/IBM/plex",
        "license": "OFL-1.1",
        "usage": (
            "The font the fakes are written in on masked images (`src/latet/vision/fonts/"
            "IBMPlexSans-Regular.ttf`), included unmodified."
        ),
        "license_file": "ibm-plex-LICENSE.txt",
    },
)

# npm projects: (directory, include transitive production dependencies).
# The dashboard bundle ships inside the wheel, so every runtime package it pulls in is
# redistributed. The docs site is build tooling that is not distributed with Latet.
NPM_PROJECTS: tuple[tuple[str, bool], ...] = (
    ("dashboard", True),
    ("docs", False),
)

# Environment markers that depend on the operating system or the machine.
_OS_MARKER = re.compile(
    r"\b(sys_platform|platform_system|os_name|platform_machine|platform_release|platform_version)\b"
)
_LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|copyright|authors?)([-._].*)?$", re.IGNORECASE)
_PROJECT_URL_KEYS = ("homepage", "home", "source", "source code", "repository", "code", "github")


class NoticeError(Exception):
    """The environment cannot produce a complete, reproducible notices file."""


@dataclass
class Entry:
    name: str
    version: str
    license: str
    homepage: str
    texts: list[tuple[str, str]] = field(default_factory=list)
    kind: str = ""


# --------------------------------------------------------------------------- text helpers


def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}text\n{text}\n{ticks}"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ") or "-"


def _link(url: str) -> str:
    return f"<{url}>" if url else "-"


def _table(entries: Iterable[Entry]) -> list[str]:
    rows = ["| Package | Version | License | Homepage |", "| --- | --- | --- | --- |"]
    rows += [f"| {_cell(e.name)} | {_cell(e.version)} | {_cell(e.license)} | {_link(e.homepage)} |" for e in entries]
    return rows


def _details(entries: Iterable[Entry]) -> list[str]:
    out: list[str] = []
    for e in entries:
        out += [
            "",
            f"### {e.name} {e.version}",
            "",
            f"- License: {e.license}",
            f"- Homepage: {_link(e.homepage)}",
        ]
        if usage := _USAGE.get(_canonical(e.name)):
            out.append(f"- Use: {usage}")
        if e.kind:
            out.append(f"- Type: {e.kind}")
        if not e.texts:
            out += [
                "",
                "No license file is shipped with this package; see its homepage for the license terms.",
            ]
        for filename, text in e.texts:
            out += ["", f"`{filename}`:", "", _fence(text)]
    return out


# --------------------------------------------------------------------------- vendored code


def vendored_section() -> list[str]:
    out = [
        "## Vendored and ported code",
        "",
        "Third-party code and fonts copied or ported into the Latet source tree.",
    ]
    for item in VENDORED:
        path = NOTICES_DIR / item["license_file"]
        if not path.is_file():
            raise NoticeError(f"missing license text {path.relative_to(ROOT)}")
        out += [
            "",
            f"### {item['name']}",
            "",
            f"- Source: {_link(item['url'])}",
            f"- License: {item['license']}",
            f"- Usage: {item['usage']}",
            "",
            _fence(_normalize(path.read_text(encoding="utf-8"))),
        ]
    return out


# --------------------------------------------------------------------------- runtime image


def runtime_image_section() -> list[str]:
    manifest = json.loads((NOTICES_DIR / "runtime-image.json").read_text(encoding="utf-8"))
    out = [
        "## Runtime-image license supplements",
        "",
        "Original license texts retained independently of the generator host's installed wheels and OS markers.",
        "Source hashes identify the original files before line-ending normalization.",
    ]
    for pending in manifest.get("pending", []):
        out += ["", f"**Pending verified input:** {pending}"]
    for item in manifest["components"]:
        out += [
            "",
            f"### {item['name']} {item['version']} (image supplement)",
            "",
            f"- License: {item['license']}",
            f"- Source: {_link(item['source'])}",
            f"- Source artifact: `{item['artifact']}`",
            f"- Artifact SHA-256: `{item['artifact_sha256']}`",
        ]
        if evidence := item.get("version_evidence"):
            out.append(f"- Version evidence: {evidence}")
        if bundled := item.get("bundled_files"):
            out += ["", "| Bundled wheel entry | SHA-256 |", "| --- | --- |"]
            out += [f"| `{file['file']}` | `{file['sha256']}` |" for file in bundled]
        for source in item["texts"]:
            path = NOTICES_DIR / source["file"]
            if not path.is_file():
                raise NoticeError(f"missing image license text {path.relative_to(ROOT)}")
            out += [
                "",
                f"`{source['source_file']}` (source SHA-256: `{source['source_sha256']}`):",
                "",
                _fence(_normalize(path.read_text(encoding="utf-8"))),
            ]
    out += [
        "",
        "### Replacing psycopg in the image",
        "",
        "Latet loads psycopg as separate, unmodified Python packages. You may replace psycopg, psycopg-binary",
        "and psycopg-pool with interface-compatible modified builds and debug those modifications, including",
        "by reverse engineering the combined work as permitted by LGPL-3.0 section 4.",
        "",
        "Build replacement wheels from your modified sources for the image's Python version, Linux platform",
        "and CPU architecture. Put the replacement wheels in a local `replacement-wheels/` directory; keep",
        "psycopg and psycopg-binary versions compatible. Use the original image's release tag below, or replace",
        "the full FROM image reference with its digest:",
        "",
        "```dockerfile",
        "FROM ghcr.io/latet-ai/latet:<release-tag>",
        "USER 0:0",
        "COPY replacement-wheels/ /tmp/psycopg-replacement/",
        "RUN python -m pip install --no-index --no-deps --force-reinstall /tmp/psycopg-replacement/*.whl \\",
        "    && rm -rf /tmp/psycopg-replacement",
        "USER 10001:10001",
        "```",
        "",
        "Build with `docker build -t latet-psycopg-replacement .` and select that image for both Latet server",
        "instances in your Compose override. Recreate both instances. Their existing readiness checks exercise",
        "the replacement's database connection. The original wheel hashes in uv.lock identify the distributed",
        "builds; they do not prevent installing your compatible replacements in this derived image.",
    ]
    return out


# --------------------------------------------------------------------------- Python


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class LockedPackage:
    name: str
    version: str
    deps: list[dict[str, object]]
    optional: dict[str, list[dict[str, object]]]
    groups: dict[str, list[dict[str, object]]]


@dataclass
class Reach:
    package: LockedPackage
    os_only: bool = True
    runtime: bool = False
    extras: set[str] = field(default_factory=set)
    markers: set[str] = field(default_factory=set)
    parents: set[str] = field(default_factory=set)


def _load_lock(path: Path) -> tuple[LockedPackage, dict[str, list[LockedPackage]]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    by_name: dict[str, list[LockedPackage]] = {}
    root: LockedPackage | None = None
    for raw in data.get("package", []):
        pkg = LockedPackage(
            name=_canonical(raw["name"]),
            version=str(raw.get("version", "")),
            deps=list(raw.get("dependencies", [])),
            optional={k: list(v) for k, v in raw.get("optional-dependencies", {}).items()},
            groups={k: list(v) for k, v in raw.get("dev-dependencies", {}).items()},
        )
        by_name.setdefault(pkg.name, []).append(pkg)
        if pkg.name == SELF:
            root = pkg
    if root is None:
        raise NoticeError(f"{path.name} has no '{SELF}' package")
    return root, by_name


def _resolve(dep: dict[str, object], by_name: dict[str, list[LockedPackage]]) -> LockedPackage | None:
    candidates = by_name.get(_canonical(str(dep["name"])), [])
    if "version" in dep:
        candidates = [c for c in candidates if c.version == dep["version"]] or candidates
    return candidates[0] if candidates else None


def _walk_lock(root: LockedPackage, by_name: dict[str, list[LockedPackage]]) -> dict[tuple[str, str], Reach]:
    """Reachability over the lock graph, tracking runtime vs dev use and OS-only paths.

    A package is "OS-only" when every path to it crosses an edge whose marker depends on the
    operating system. States only ever improve (OS-only -> everywhere, dev -> runtime, more
    extras), and a package is revisited only when its state improves, so this terminates.
    """
    reach: dict[tuple[str, str], Reach] = {}
    queue: list[tuple[str, str]] = []

    def push(
        dep: dict[str, object], parent: str, os_only: bool, runtime: bool, through: frozenset[str] = frozenset()
    ) -> None:
        target = _resolve(dep, by_name)
        if target is None or target.name == SELF:
            return
        marker = str(dep.get("marker", ""))
        child_os = os_only or bool(_OS_MARKER.search(marker))
        extras = {str(x) for x in dep.get("extra", [])}  # type: ignore[attr-defined]
        if target.name in OWN:  # not listed: what it needs is reached as if `parent` needed it
            if target.name not in through:
                edges = list(target.deps)
                for extra in sorted(extras):
                    edges += target.optional.get(extra, [])
                for edge in edges:
                    push(edge, parent, child_os, runtime, through | {target.name})
            return
        key = (target.name, target.version)
        new = key not in reach
        state = reach.setdefault(key, Reach(target))
        state.parents.add(parent)
        if child_os and marker:
            state.markers.add(marker)
        improved = (state.os_only and not child_os) or (runtime and not state.runtime) or not extras <= state.extras
        state.os_only = state.os_only and child_os
        state.runtime = state.runtime or runtime
        state.extras |= extras
        if new or improved:
            queue.append(key)

    for dep in root.deps:
        push(dep, SELF, False, True)
    for deps in root.optional.values():
        for dep in deps:
            push(dep, SELF, False, True)
    for deps in root.groups.values():
        for dep in deps:
            push(dep, SELF, False, False)

    while queue:
        state = reach[queue.pop()]
        pkg = state.package
        edges = list(pkg.deps)
        for extra in sorted(state.extras):
            edges += pkg.optional.get(extra, [])
        for dep in edges:
            push(dep, pkg.name, state.os_only, state.runtime)
    return reach


def _dist_license(meta: md.PackageMetadata) -> str:
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    parts: list[str] = []
    field_value = (meta.get("License") or "").strip()
    if field_value and "\n" not in field_value and len(field_value) <= 80 and field_value.upper() != "UNKNOWN":
        parts.append(field_value)
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            label = classifier.split(" :: ")[-1]
            if label not in parts:
                parts.append(label)
    return "; ".join(parts) or "See license text"


def _dist_homepage(meta: md.PackageMetadata) -> str:
    home = (meta.get("Home-page") or "").strip()
    if home and home.upper() != "UNKNOWN":
        return home
    urls: dict[str, str] = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls.setdefault(label.strip().lower(), url.strip())
    for key in _PROJECT_URL_KEYS:
        if urls.get(key):
            return urls[key]
    return next(iter(urls.values()), "")


def _dist_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for file in dist.files or []:
        parts = file.parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        inner = parts[1:]
        if (inner[0] == "licenses" and len(inner) > 1) or (len(inner) == 1 and _LICENSE_NAME.match(inner[0])):
            found["/".join(inner)] = str(file)
    if not found:
        for declared in dist.metadata.get_all("License-File") or []:
            for candidate in (f"licenses/{declared}", declared):
                if dist.read_text(candidate) is not None:
                    found[candidate] = candidate
                    break
    texts: list[tuple[str, str]] = []
    for display in sorted(found, key=str.lower):
        location = found[display]
        if "/" in location and location.split("/")[0].endswith(".dist-info"):
            raw = Path(str(dist.locate_file(location))).read_bytes().decode("utf-8", errors="replace")
        else:
            raw = dist.read_text(location) or ""
        text = _normalize(_without_wheel_appendix(raw, _canonical(dist.metadata["Name"])))
        if text:
            texts.append((display.removeprefix("licenses/"), text))
    return texts


# Binary wheels (NumPy's, for one) append the licenses of the libraries they bundle, which differ
# from one platform to the next: keep the project's own text, so this file is the same everywhere.
_WHEEL_APPENDIX = re.compile(r"^This binary distribution of .+ (also )?bundles", re.MULTILINE)
# Pillow's wheels add a section per bundled library after its own license: "===== brotli-1.2.0 ====="
# on Windows, a "----" rule and the library's name on Linux and macOS.
_APPENDIX_BY_PACKAGE = {"pillow": re.compile(r"^(?:===== \S.* =====|-{4,})$", re.MULTILINE)}


def _without_wheel_appendix(raw: str, name: str = "") -> str:
    match = _WHEEL_APPENDIX.search(raw)
    if match is None and name in _APPENDIX_BY_PACKAGE:
        match = _APPENDIX_BY_PACKAGE[name].search(raw)
    if match is None:
        return raw
    head = raw[: match.start()].rstrip().rstrip("-").rstrip()
    return head + _WHEEL_NOTE


_WHEEL_NOTE = """

(Binary wheels of this project also bundle compiled libraries, which vary by platform; their
licenses are listed in the license file inside the installed package.)
"""

# How a package is used, where its license asks it to be said: the LGPL's, used as separate, unmodified packages.
_USAGE = {
    name: "Installed with `latet[server]` and used as a separate, unmodified package: the LGPL-3.0 lets anyone "
    "replace it with another build of psycopg 3."
    for name in ("psycopg", "psycopg-binary", "psycopg-pool")
}


def _dist_entry(name: str, version: str) -> Entry:
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        raise NoticeError(
            f"{name} {version} is in uv.lock but not installed; run `uv sync --all-extras` first"
        ) from None
    meta = dist.metadata
    installed = meta["Version"]
    if installed != version:
        raise NoticeError(f"{name}: uv.lock pins {version} but {installed} is installed; run `uv sync` first")
    return Entry(meta["Name"], installed, _dist_license(meta), _dist_homepage(meta), _dist_texts(dist))


def python_sections() -> list[str]:
    lock = ROOT / "uv.lock"
    if not lock.is_file():
        entries = sorted(
            (
                Entry(
                    d.metadata["Name"],
                    d.metadata["Version"],
                    _dist_license(d.metadata),
                    _dist_homepage(d.metadata),
                    _dist_texts(d),
                )
                for d in md.distributions()
                if _canonical(d.metadata["Name"]) not in OWN
            ),
            key=lambda e: _canonical(e.name),
        )
        return ["## Python packages", "", *_table(entries), *_details(entries)]

    root, by_name = _load_lock(lock)
    reach = _walk_lock(root, by_name)
    runtime: list[Entry] = []
    dev: list[Entry] = []
    platform: list[tuple[str, str, Reach]] = []
    for (name, version), state in sorted(reach.items()):
        if state.os_only:
            platform.append((name, version, state))
        elif state.runtime:
            runtime.append(_dist_entry(name, version))
        else:
            dev.append(_dist_entry(name, version))

    out = [
        "## Python runtime dependencies",
        "",
        "Installed together with Latet (directly or as dependencies of its dependencies).",
        "",
        *_table(runtime),
        *_details(runtime),
        "",
        "## Python development dependencies",
        "",
        "Used to develop and test Latet. They are not distributed with it.",
        "",
        *_table(dev),
        *_details(dev),
    ]
    if platform:
        out += [
            "",
            "## Platform-specific Python dependencies",
            "",
            "Installed only on some operating systems. They are listed without license texts so that this",
            "file is the same on every platform; see each package's metadata for its license terms.",
            "",
            "| Package | Version | Installed when | Required by | Used for |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, version, state in platform:
            markers = " or ".join(f"`{m}`" for m in sorted(state.markers)) or "-"
            parents = ", ".join(sorted(state.parents))
            usage = "runtime" if state.runtime else "development"
            out.append(f"| {name} | {version} | {_cell(markers)} | {_cell(parents)} | {usage} |")
    return out


# --------------------------------------------------------------------------- npm


def _npm_license(manifest: dict[str, object]) -> str:
    value = manifest.get("license")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict) and value.get("type"):
        return str(value["type"])
    legacy = manifest.get("licenses")
    if isinstance(legacy, list):
        types = [str(x.get("type")) for x in legacy if isinstance(x, dict) and x.get("type")]
        if types:
            return " OR ".join(types)
    return "See license text"


def _npm_homepage(manifest: dict[str, object]) -> str:
    homepage = manifest.get("homepage")
    if isinstance(homepage, str) and homepage.strip():
        return homepage.strip()
    repo = manifest.get("repository")
    url = repo.get("url") if isinstance(repo, dict) else repo
    if not isinstance(url, str) or not url:
        return ""
    url = re.sub(r"^git\+", "", url)
    url = re.sub(r"^git://", "https://", url)
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    url = re.sub(r"^github:", "https://github.com/", url)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        url = f"https://github.com/{url}"
    return re.sub(r"\.git$", "", url)


def _npm_find(name: str, start: Path, project: Path) -> Path | None:
    """Node's lookup: the nearest node_modules/<name> from ``start`` up to the project root."""
    current = start
    while True:
        candidate = current / "node_modules" / name
        if (candidate / "package.json").is_file():
            return candidate
        if current == project or project not in current.parents:
            return None
        current = current.parent
        while current.name == "node_modules" or (
            current.name.startswith("@") and current.parent.name == "node_modules"
        ):
            current = current.parent


def _npm_entry(package_dir: Path, kind: str) -> Entry:
    manifest = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    texts = []
    for path in sorted(package_dir.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and _LICENSE_NAME.match(path.name):
            text = _normalize(path.read_bytes().decode("utf-8", errors="replace"))
            if text:
                texts.append((path.name, text))
    return Entry(
        str(manifest.get("name", package_dir.name)),
        str(manifest.get("version", "")),
        _npm_license(manifest),
        _npm_homepage(manifest),
        texts,
        kind,
    )


def _npm_project(directory: str, transitive: bool) -> list[str]:
    project = ROOT / directory
    manifest_path = project / "package.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = sorted(manifest.get("dependencies", {}))
    dev = sorted(manifest.get("devDependencies", {}))
    title = f"## npm packages: {directory}/"
    if not (project / "node_modules").is_dir():
        print(f"warning: {directory}/node_modules is missing; run `npm ci` in {directory}/", file=sys.stderr)
        return [
            title,
            "",
            f"Not listed: `{directory}/node_modules` was not installed when this file was generated.",
        ]

    entries: dict[tuple[str, str], Entry] = {}
    queue: list[tuple[str, Path, str]] = [(name, project, "runtime dependency") for name in runtime]
    queue += [(name, project, "development dependency") for name in dev]
    while queue:
        name, start, kind = queue.pop(0)
        package_dir = _npm_find(name, start, project)
        if package_dir is None:
            raise NoticeError(f"{directory}: npm package {name} is not installed; run `npm ci` in {directory}/")
        entry = _npm_entry(package_dir, kind)
        if (entry.name, entry.version) in entries:
            continue
        entries[(entry.name, entry.version)] = entry
        if transitive and kind != "development dependency":
            child = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
            for dep in sorted(child.get("dependencies", {})):
                queue.append((dep, package_dir, "runtime dependency (transitive)"))

    ordered = sorted(entries.values(), key=lambda e: (e.name.lower(), e.version))
    scope = (
        "Declared in `package.json`, plus the packages they depend on at runtime."
        if transitive
        else "Declared in `package.json` (transitive dependencies are not listed)."
    )
    return [title, "", scope, "", *_table(ordered), *_details(ordered)]


def npm_sections() -> list[str]:
    out: list[str] = []
    for directory, transitive in NPM_PROJECTS:
        section = _npm_project(directory, transitive)
        if section:
            out += ["", *section]
    return out


# --------------------------------------------------------------------------- main


def render() -> str:
    header = [
        "# Third-party notices",
        "",
        "Latet is licensed under the Latet Sustainable Use License (see `LICENSE` and `LICENSING.md`), its",
        "`ee` directories under the Latet Enterprise License (`ee/LICENSE`). This file lists the third-party",
        "software it includes, ports or depends on, with the license of each component.",
        "",
        "Generated by `scripts/gen_notices.py`; do not edit by hand. To regenerate:",
        "",
        "```sh",
        "uv run python scripts/gen_notices.py",
        "```",
        "",
    ]
    body = [*vendored_section(), "", *runtime_image_section(), "", *python_sections(), *npm_sections()]
    return "\n".join(header + body).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate THIRD_PARTY_NOTICES.md.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is missing or out of date")
    args = parser.parse_args(argv)
    try:
        content = render()
    except NoticeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8").replace("\r\n", "\n") if OUTPUT.is_file() else ""
        if current == content:
            print(f"{OUTPUT.name} is up to date.")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(), content.splitlines(), "committed", "generated", lineterm="", n=1
        )
        for line in list(diff)[:60]:
            print(line.encode("ascii", "replace").decode("ascii"), file=sys.stderr)
        print(f"{OUTPUT.name} is out of date: run `uv run python scripts/gen_notices.py`.", file=sys.stderr)
        return 1

    OUTPUT.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({len(content.encode('utf-8')):,} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
