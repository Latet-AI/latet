"""The installers: the wheel of the latest release on GitHub, with local name detection and the text in images, and
never PyPI. They run here with stand-ins for curl, uv and latet in a folder of their own, first on a PATH of
system folders only: nothing is downloaded or installed, and no Latet of yours is stopped."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WHEEL = "https://github.com/Latet-AI/latet/releases/download/v1.2.0/latet-1.2.0-py3-none-any.whl"
RELEASE = {
    "tag_name": "v1.2.0",
    "assets": [
        {"name": "latet-1.2.0.tar.gz", "browser_download_url": WHEEL.replace("-py3-none-any.whl", ".tar.gz")},
        {"name": "latet-1.2.0-py3-none-any.whl", "browser_download_url": WHEEL},
    ],
}
ELSEWHERE = {
    "tag_name": "v1.2.0",
    "assets": [{"name": "latet.whl", "browser_download_url": "https://evil.example/latet-1.2.0-py3-none-any.whl"}],
}
TUTORIAL = "New to Latet? 'latet tutorial' shows it at work in about 30 minutes."


def _tutorial_first(said: str) -> bool:
    """Whether the installer's last message names the tutorial on the line before the one with `latet claude`."""
    lines = said.splitlines()
    claude = next(number for number, line in enumerate(lines) if "'latet claude'" in line)
    return lines[claude - 1].endswith(TUTORIAL)


def _posix_sh() -> tuple[str, list[str]] | None:
    """A POSIX sh, and the system folders its tools (sed, head, cat) are in; None where there is none. On Windows,
    Git's own sh, not its launcher (which puts Git's folders, and their curl, before the stand-ins)."""
    if sys.platform == "win32":
        git = shutil.which("git")
        found = [p / "usr" / "bin" / "sh.exe" for p in Path(git).resolve().parents] if git else []
        sh = next((candidate for candidate in found if candidate.is_file()), None)
        return (str(sh), [str(sh.parent)]) if sh is not None else None
    sh = shutil.which("sh", path="/usr/bin:/bin")
    return (sh, ["/usr/bin", "/bin"]) if sh else None


SH = _posix_sh()


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode())
    path.chmod(0o755)


class Stand:
    """The stand-ins' folder: GitHub's answer (None: no release), and what curl, uv and latet were asked."""

    def __init__(self, tmp_path: Path, release: object | None) -> None:
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.tools = tmp_path / "tools"  # where uv says the tools' commands are
        self.temp = tmp_path / "tmp"
        self.home = Path(tempfile.mkdtemp(prefix="installer-", dir=os.environ["HOME"]))
        self.bin.mkdir()
        self.tools.mkdir()
        self.temp.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.write_text("")
        self.argv = tmp_path / "install-argv.log"  # every argument of each `uv tool install`, one call a line
        self.argv.write_text("")
        self.answer = tmp_path / "release.json"
        if release is not None:
            self.answer.write_bytes(json.dumps(release, indent=2).encode())
        self.failing = tmp_path / "failing.txt"  # a requirement uv cannot install, one per line
        self.failing.write_text("")
        self.listing = tmp_path / "listing.txt"  # what `uv tool list --show-extras` prints: no Latet yet
        self.listing.write_text("")
        self.old = tmp_path / "old-uv"  # there: a uv too old for --show-extras
        self.status = tmp_path / "status.txt"  # there: the API answers this status, with no release in it
        self.page = tmp_path / "page.txt"  # there: where GitHub redirects the page of the latest release
        self.offline = tmp_path / "offline"  # there: GitHub cannot be reached

    def answers(self, status: int) -> None:
        """GitHub's API answers ``status`` (403: its limit of 60 requests an hour from one address)."""
        self.status.write_text(str(status))

    def redirects(self, to: str) -> None:
        """The page of the latest release redirects to ``to``."""
        self.page.write_text(to)

    def fail(self, requirement: str) -> None:
        """uv cannot install ``requirement``."""
        self.failing.write_bytes(f"{requirement}\n".encode())  # LF: sh reads it too

    def installed(self, extras: str = "") -> None:
        """A Latet is installed already, with these extras (``ner, ocr``), or with none."""
        line = f"latet v1.1.0 [extras: {extras}]" if extras else "latet v1.1.0"
        self.listing.write_bytes(f"{line}\n- latet\n- latet-daemon\n".encode())

    def old_uv(self) -> None:
        """uv cannot say which extras a tool has."""
        self.old.write_text("")

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def installs(self) -> list[str]:
        return [line.removeprefix("uv tool install ") for line in self.calls() if line.startswith("uv tool install ")]

    def install_arguments(self) -> list[str]:
        return [line for line in self.argv.read_text().splitlines() if line]

    def homes(self) -> dict[str, str]:
        paths = {
            "HOME": self.home,
            "USERPROFILE": self.home,
            "CLAUDE_CONFIG_DIR": self.home / ".claude",
            "CODEX_HOME": self.home / ".codex",
            "XDG_CONFIG_HOME": self.home / ".config",
            "APPDATA": self.home / "roaming",
            "LOCALAPPDATA": self.home / "local",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        environment = {name: str(path) for name, path in paths.items()}
        if os.name == "nt":
            home = paths["USERPROFILE"]
            environment.update(HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive) :])
        return environment


@pytest.fixture
def sh_stand(tmp_path):
    if SH is None:
        pytest.skip("no POSIX sh")

    def make(release: object | None = RELEASE) -> Stand:
        stand = Stand(tmp_path, release)
        log, answer, failing, tools, listing, old, status, page, offline = (
            p.as_posix()
            for p in (
                stand.log, stand.answer, stand.failing, stand.tools, stand.listing, stand.old, stand.status,
                stand.page, stand.offline,
            )
        )  # fmt: skip
        # curl: -o FILE (the body), -w (the status), -I (the headers: the page's redirect); the URL is logged.
        _write(
            stand.bin / "curl",
            "#!/bin/sh\n"
            'out=""; head=""; said=""; url=""\n'
            'while [ $# -gt 0 ]; do\n  case "$1" in\n'
            '    -o) out="$2"; shift ;;\n    -w) said=1; shift ;;\n    -I) head=1 ;;\n    -*) ;;\n    *) url="$1" ;;\n'
            "  esac\n  shift\ndone\n"
            f'echo "curl $url" >> "{log}"\n'
            f'if [ -f "{offline}" ]; then [ -n "$said" ] && printf 000; exit 6; fi\n'
            'if [ -n "$head" ]; then\n'
            f'  if [ -f "{page}" ]; then printf "HTTP/2 302\\r\\nlocation: %s\\r\\n\\r\\n" "$(cat "{page}")";\n'
            '  else printf "HTTP/2 404\\r\\n\\r\\n"; fi\n  exit 0\nfi\n'
            f'if [ -f "{answer}" ]; then code=200; else code=404; fi\n'
            f'[ -f "{status}" ] && code="$(cat "{status}")"\n'
            f'if [ "$code" = 200 ]; then body="$(cat "{answer}")"; else body=\'{{"message": "no"}}\'; fi\n'
            'if [ -n "$out" ]; then printf "%s\\n" "$body" > "$out"; else [ "$code" = 200 ] || exit 22; printf "%s\\n" "$body"; fi\n'
            '[ -n "$said" ] && printf "%s" "$code"\n'
            "exit 0\n",
        )
        _write(
            stand.bin / "uv",
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            f'"tool install") echo "$*" >> "{stand.argv}"; for wanted; do :; done; echo "uv tool install $wanted" >> "{log}"\n'
            f'  while IFS= read -r no; do [ "$no" = "$wanted" ] && exit 1; done < "{failing}"; exit 0 ;;\n'
            f'"tool dir") echo "{tools}" ;;\n'
            f'"tool list") [ -f "{old}" ] && exit 2; cat "{listing}" ;;\n'
            "esac\n",
        )
        _write(stand.tools / "latet", '#!/bin/sh\necho "latet 1.2.0"\n')
        return stand

    return make


def _run_sh(stand: Stand, **env: str):
    assert SH is not None
    sh, system = SH
    environ = {
        "PATH": os.pathsep.join([str(stand.bin), *system]),
        **stand.homes(),
        "TMPDIR": stand.temp.as_posix(),
        "TMP": str(stand.temp),
        "TEMP": str(stand.temp),
        "LATET_NO_MODIFY_PATH": "1",
        **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {}),
        **env,
    }
    return subprocess.run(
        [sh, str(SCRIPTS / "install.sh")], env=environ, capture_output=True, text=True, timeout=60, check=False
    )


def test_install_sh_installs_the_latest_release_with_names_and_images(sh_stand):
    stand = sh_stand()
    done = _run_sh(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]
    assert stand.calls()[0] == "curl https://api.github.com/repos/Latet-AI/latet/releases/latest"
    assert "pypi" not in (done.stdout + done.stderr).lower()
    assert "latet ner install" in done.stdout and "latet ocr install" in done.stdout  # their models: next
    assert _tutorial_first(done.stdout)  # whoever is new to Latet sees it at work before their agent


def test_install_sh_says_to_open_a_new_terminal_when_this_one_cannot_find_latet(sh_stand):
    """uv adds its tool folder to the PATH of the terminals opened next: the one the script ran in finds no `latet`
    until then, and the script says so; with LATET_NO_MODIFY_PATH, it says where Latet is instead."""
    stand = sh_stand()
    done = _run_sh(stand, LATET_NO_MODIFY_PATH="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Open a new terminal" in done.stdout
    kept = _run_sh(stand)  # LATET_NO_MODIFY_PATH=1: the PATH is the user's to change
    assert "Open a new terminal" not in kept.stdout and stand.tools.as_posix() in kept.stdout
    found = _run_sh(stand, LATET_NO_MODIFY_PATH="", PATH=os.pathsep.join([str(stand.bin), str(stand.tools), *SH[1]]))
    assert "Open a new terminal" not in found.stdout and "not on your PATH" not in found.stdout


OFF = "Off: local name detection and the text in images, which uv could not install here. 'latet upgrade' tries again."


@pytest.mark.parametrize("before", [None, ""])  # no Latet yet; one with neither
def test_install_sh_goes_on_without_names_and_images_where_they_cannot_be_installed(sh_stand, before):
    stand = sh_stand()
    if before is not None:
        stand.installed(before)
    stand.fail(f"latet[ner,ocr] @ {WHEEL}")
    done = _run_sh(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}", f"latet @ {WHEEL}"]
    assert "without them" in done.stdout and "latet ner install" not in done.stdout
    assert done.stdout.rstrip().splitlines()[-1] == OFF  # the run ends saying what is off


@pytest.mark.parametrize("extras", ["ner, ocr", "ner", "ocr"])
def test_install_sh_never_takes_away_names_or_images_an_installed_latet_has(sh_stand, extras):
    """uv could not install the release with them: installing it without them would uninstall what is there."""
    stand = sh_stand()
    stand.installed(extras)
    stand.fail(f"latet[ner,ocr] @ {WHEEL}")
    done = _run_sh(stand)
    assert done.returncode != 0
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]
    assert "left as it was" in done.stderr, done.stdout + done.stderr


def test_install_sh_with_a_uv_too_old_to_say_keeps_an_latet_as_it_was(sh_stand):
    stand = sh_stand()
    stand.old_uv()
    _write(stand.bin / "latet", f'#!/bin/sh\necho "latet $*" >> "{stand.log.as_posix()}"\n')  # one is installed
    stand.fail(f"latet[ner,ocr] @ {WHEEL}")
    done = _run_sh(stand)
    assert done.returncode != 0 and "left as it was" in done.stderr, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]


PAGE = "https://github.com/Latet-AI/latet/releases"
LIMIT = "GitHub's API answered 403 (it takes 60 requests an hour from one address)"


def _looked_up_nothing(done, why: str) -> None:
    """The installer stopped before installing, saying why, where the releases are, and how to install one by hand."""
    said = done.stdout + done.stderr
    assert done.returncode != 0, said
    assert f"Could not look up the latest release of Latet: {why}." in said, said
    assert f"The releases are at {PAGE}." in said and "LATET_SOURCE" in said, said


@pytest.mark.parametrize(
    ("release", "why"),
    [(None, "there is no release of Latet yet"), (ELSEWHERE, "the latest release has no wheel of Latet")],
)  # none at all; a wheel that is not Latet's own
def test_install_sh_with_no_release_installs_nothing(sh_stand, release, why):
    stand = sh_stand(release)
    _looked_up_nothing(_run_sh(stand), why)
    assert stand.installs() == []


def test_install_sh_says_why_github_did_not_answer(sh_stand):
    stand = sh_stand()
    stand.answers(403)  # and the page of the latest release names none
    _looked_up_nothing(_run_sh(stand), LIMIT)
    stand.offline.write_text("")
    _looked_up_nothing(_run_sh(stand), "GitHub could not be reached (no connection, or a proxy in the way)")
    assert stand.installs() == []


def test_install_sh_reads_the_page_of_the_latest_release_where_the_api_does_not_answer(sh_stand):
    stand = sh_stand()
    stand.answers(403)
    stand.redirects(f"{PAGE}/tag/v1.2.0")
    done = _run_sh(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]  # named after the tag, as the release makes it
    assert f"{LIMIT}: the latest release is read from its page instead." in done.stdout
    assert stand.calls()[1] == f"curl {PAGE}/latest"


def test_install_sh_builds_latet_again_from_what_latet_source_names(sh_stand):
    """A clone keeps its version while its code changes: uv would keep the build it installed before ("Audited")."""
    stand = sh_stand()
    done = _run_sh(stand, LATET_SOURCE="latet[ner,ocr] @ file:///work/latet")
    assert done.returncode == 0, done.stdout + done.stderr
    assert all("--reinstall-package latet" in line for line in stand.install_arguments()), stand.install_arguments()


def test_install_sh_takes_a_release_wheel_as_it_is(sh_stand):
    stand = sh_stand()
    done = _run_sh(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.install_arguments() and not any("--reinstall" in line for line in stand.install_arguments())


def test_install_sh_installs_what_latet_source_names_and_asks_github_nothing(sh_stand):
    stand = sh_stand()
    done = _run_sh(stand, LATET_SOURCE="dist/latet-1.2.0-py3-none-any.whl")
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.calls() == ["uv tool install dist/latet-1.2.0-py3-none-any.whl"]
    assert "latet ner install" not in done.stdout  # with or without the extras: not known


@pytest.mark.parametrize(
    ("source", "named"),
    [
        ("latet[ner,ocr] @ file:///work/latet", ["latet ner install", "latet ocr install"]),
        ("latet[ner] @ file:///work/latet", ["latet ner install"]),
        ("latet[ocr] @ file:///work/latet", ["latet ocr install"]),
    ],
    ids=["both", "names", "images"],
)
def test_install_sh_recommends_the_models_of_the_extras_latet_source_names(sh_stand, source, named):
    """A source that names its extras (a clone installed as the docs say) gets the advice the release gets, for the
    models of those extras alone."""
    stand = sh_stand()
    done = _run_sh(stand, LATET_SOURCE=source)
    assert done.returncode == 0, done.stdout + done.stderr
    for command in ("latet ner install", "latet ocr install"):
        assert (command in done.stdout) == (command in named), done.stdout


# --- install.ps1, on Windows ------------------------------------------------------------------------------

POWERSHELL = (shutil.which("pwsh") or shutil.which("powershell")) if sys.platform == "win32" else None
# Where both are here: Windows PowerShell 5.1, where `irm | iex` runs by default, and pwsh. They differ in what they
# make of a program's stderr.
SHELLS = [shell for shell in (shutil.which("powershell"), shutil.which("pwsh")) if shell] if POWERSHELL else []


@pytest.fixture
def ps_stand(tmp_path):
    if POWERSHELL is None:
        pytest.skip("install.ps1 runs on Windows, with PowerShell")

    def make(release: object | None = RELEASE) -> Stand:
        stand = Stand(tmp_path, release)
        log, failing, tools, listing, old = (
            str(p) for p in (stand.log, stand.failing, stand.tools, stand.listing, stand.old)
        )
        _write(
            stand.bin / "uv.ps1",
            f"$log = '{log}'\n"
            "if ($args[0] -eq 'tool' -and $args[1] -eq 'install') {\n"
            f"  Add-Content '{stand.argv}' ($args -join ' ')\n"
            '  $wanted = $args[-1]; Add-Content $log "uv tool install $wanted"\n'
            f"  if ((Get-Content '{failing}') -contains $wanted) {{ exit 1 }}\n"
            "  exit 0\n"
            "}\n"
            f"if ($args[0] -eq 'tool' -and $args[1] -eq 'dir') {{ '{tools}' }}\n"
            "if ($args[0] -eq 'tool' -and $args[1] -eq 'list') {\n"
            f"  if (Test-Path '{old}') {{ exit 2 }}\n"
            f"  Get-Content '{listing}'\n"
            "}\n"
            # What uv says of a folder on PATH already, on stderr, as a program does.
            "if ($args[0] -eq 'tool' -and $args[1] -eq 'update-shell') {\n"
            '  Add-Content $log "uv tool update-shell"\n'
            '  & cmd.exe /c "echo Executable directory is already in PATH 1>&2"\n'
            "}\n"
            "exit 0\n",
        )
        # The version check runs latet.exe: any program of Windows' that ends at once does.
        shutil.copy(Path(os.environ["SYSTEMROOT"]) / "System32" / "where.exe", stand.tools / "latet.exe")
        return stand

    return make


def _run_ps(stand: Stand, shell: str | None = None, **env: str):
    shell = shell or POWERSHELL
    assert shell is not None
    system32 = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    # GitHub's API, as Invoke-RestMethod reads it: the saved answer, or the error of a status (with the response's
    # status code, as PowerShell's own error carries it), or no answer at all. The page of the latest release, as
    # Invoke-WebRequest follows its redirect.
    fake = (
        "function Invoke-RestMethod { param([string]$Uri)\n"
        f"  Add-Content '{stand.log}' \"irm $Uri\"\n"
        f"  if (Test-Path '{stand.offline}') {{ throw [System.Net.WebException]::new('No such host is known.') }}\n"
        f"  $code = if (Test-Path '{stand.status}') {{ [int](Get-Content '{stand.status}') }}"
        f" elseif (Test-Path '{stand.answer}') {{ 200 }} else {{ 404 }}\n"
        "  if ($code -ne 200) {\n"
        '    $failure = [System.Exception]::new("Response status code does not indicate success: $code.")\n'
        "    Add-Member -InputObject $failure -NotePropertyName Response -NotePropertyValue ([pscustomobject]@{ StatusCode = $code })\n"
        "    throw $failure\n"
        "  }\n"
        f"  Get-Content -Raw '{stand.answer}' | ConvertFrom-Json\n"
        "}\n"
        "function Invoke-WebRequest { param([string]$Uri, [switch]$UseBasicParsing)\n"
        f"  Add-Content '{stand.log}' \"iwr $Uri\"\n"
        f"  if (-not (Test-Path '{stand.page}')) {{ throw 'Not Found' }}\n"
        f"  [pscustomobject]@{{ BaseResponse = [pscustomobject]@{{ ResponseUri = (Get-Content -Raw '{stand.page}').Trim() }} }}\n"
        "}\n"
        # What the script throws, whole: PowerShell's own error view wraps it at the console's width.
        f"try {{ & '{SCRIPTS / 'install.ps1'}' }} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}\n"
    )
    # PATHEXT, as every Windows has it: without it, Windows PowerShell 5.1 takes a program's stderr as no shell of a
    # user takes it (never an error).
    kept = {"SYSTEMROOT", "WINDIR", "PATHEXT"}
    environ = {
        **{k: v for k, v in os.environ.items() if k.upper() in kept},
        "PATH": os.pathsep.join([str(stand.bin), system32]),
        **stand.homes(),
        "PSModuleAnalysisCachePath": str(stand.home / "local" / "ModuleAnalysisCache"),
        "TMPDIR": stand.temp.as_posix(),
        "TMP": str(stand.temp),
        "TEMP": str(stand.temp),
        "LATET_NO_MODIFY_PATH": "1",
        **env,
    }
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", fake],
        cwd=stand.root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_install_ps1_installs_the_latest_release_with_names_and_images(ps_stand):
    stand = ps_stand()
    done = _run_ps(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]
    assert stand.calls()[0] == "irm https://api.github.com/repos/Latet-AI/latet/releases/latest"
    assert "latet ner install" in done.stdout and "latet ocr install" in done.stdout
    assert _tutorial_first(done.stdout)


@pytest.mark.parametrize("shell", SHELLS or [None], ids=lambda shell: Path(shell).stem if shell else "none")
def test_powershell_installer_keeps_every_home_in_the_stand(ps_stand, shell):
    stand = ps_stand()
    report = stand.root / "environment.json"
    uv = stand.bin / "uv.ps1"
    original = uv.read_text()
    uv.write_text(
        f"[Environment]::GetEnvironmentVariables() | ConvertTo-Json | Set-Content '{report}'\n" + original,
        encoding="utf-8",
    )
    done = _run_ps(stand, shell, LATET_SOURCE="stand-in.whl")
    assert done.returncode == 0, done.stdout + done.stderr
    environment = json.loads(report.read_text(encoding="utf-8-sig"))
    for name in (
        "HOME",
        "USERPROFILE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        path = Path(environment[name])
        assert path.is_absolute() and path.is_relative_to(stand.home), name
    local = Path(environment["LOCALAPPDATA"])
    assert local.is_dir()


@pytest.mark.parametrize("shell", SHELLS or [None], ids=lambda shell: Path(shell).stem if shell else "none")
def test_powershell_installer_contains_native_cache_and_relative_output(ps_stand, shell, monkeypatch):
    """Removing either the cwd or cache override must produce output in the separate owned control folder."""
    if shell is None:
        pytest.skip("install.ps1 runs on Windows, with PowerShell")
    stand = ps_stand()
    outer = stand.root / "outer-host"
    outer.mkdir()
    monkeypatch.chdir(outer)
    report = stand.root / "native-output.json"
    safety_cache = outer / "cache" / "ModuleAnalysisCache"
    safety_cache.parent.mkdir()
    expected_cache = stand.home / "local" / "ModuleAnalysisCache"
    module = stand.root / "Eop32Owned.psm1"
    module.write_bytes(b"function Invoke-Eop32Owned { 'owned' }\nExport-ModuleMember -Function Invoke-Eop32Owned\n")
    uv = stand.bin / "uv.ps1"
    original = uv.read_text()
    uv.write_bytes(
        (
            f"if (-not [IO.File]::Exists('{report}')) {{\n"
            "  [IO.File]::WriteAllText('eop32-relative-output.txt', 'owned')\n"
            f"  Import-Module '{module}' -Force -ErrorAction Stop\n"
            "  $null = Get-Command Invoke-Eop32Owned -ErrorAction Stop\n"
            "  $watch = [Diagnostics.Stopwatch]::StartNew()\n"
            "  while ($watch.ElapsedMilliseconds -lt 20000) {\n"
            "    if ([IO.File]::Exists($env:PSModuleAnalysisCachePath) -and\n"
            "        ([IO.FileInfo]$env:PSModuleAnalysisCachePath).Length -gt 0) { break }\n"
            "    [Threading.Thread]::Sleep(100)\n"
            "  }\n"
            "  $cacheExists = [IO.File]::Exists($env:PSModuleAnalysisCachePath)\n"
            "  $metadata = [ordered]@{\n"
            "    Version = $PSVersionTable.PSVersion.ToString()\n"
            "    Location = (Get-Location).Path\n"
            "    NativeCwd = [IO.Directory]::GetCurrentDirectory()\n"
            "    LocalApplicationData = [Environment]::GetFolderPath('LocalApplicationData')\n"
            "    CachePath = $env:PSModuleAnalysisCachePath\n"
            "    CacheExists = $cacheExists\n"
            "    CacheBytes = if ($cacheExists) { ([IO.FileInfo]$env:PSModuleAnalysisCachePath).Length } else { 0 }\n"
            "    WaitMilliseconds = $watch.ElapsedMilliseconds\n"
            "  }\n"
            f"  $metadata | ConvertTo-Json | Set-Content '{report}' -Encoding UTF8\n"
            "}\n" + original
        ).encode()
    )
    run = subprocess.run

    def safely_run(arguments, **kwargs):
        # RED and removal mutations must never let an unknown native fallback touch a user's cache. This only
        # supplies an owned control destination when the harness omits its override; the actual child still runs.
        environment = dict(kwargs["env"])
        environment.setdefault("PSModuleAnalysisCachePath", str(safety_cache))
        cache = Path(environment["PSModuleAnalysisCachePath"])
        assert cache.is_absolute() and (
            cache.resolve().is_relative_to(stand.root.resolve()) or cache.resolve().is_relative_to(stand.home.resolve())
        )
        cwd = Path(kwargs.get("cwd", Path.cwd())).resolve()
        assert cwd.is_relative_to(stand.root)
        assert "-NoProfile" in arguments and "-NonInteractive" in arguments
        environment["PSModulePath"] = str(Path(shell).parent / "Modules")
        environment["POWERSHELL_TELEMETRY_OPTOUT"] = "1"
        environment["POWERSHELL_UPDATECHECK"] = "Off"
        return run(arguments, **{**kwargs, "env": environment, "timeout": 45})

    monkeypatch.setattr(subprocess, "run", safely_run)
    done = _run_ps(stand, shell, LATET_SOURCE="stand-in.whl")
    assert done.returncode == 0, done.stdout + done.stderr
    metadata = json.loads(report.read_text(encoding="utf-8-sig"))
    print(json.dumps(metadata, sort_keys=True))  # Own report and cache metadata only; never cache contents.
    assert metadata["CacheExists"] and metadata["CacheBytes"] > 0, "native cache not observed within 20 seconds"
    failures = []
    if not (stand.root / "eop32-relative-output.txt").is_file():
        failures.append("relative output escaped the stand working directory")
    if (outer / "eop32-relative-output.txt").exists():
        failures.append("relative output reached the owned outer-host control")
    if not expected_cache.is_file() or expected_cache.stat().st_size == 0:
        failures.append("native cache missing from the stand's explicit cache destination")
    if safety_cache.exists():
        failures.append("native cache reached the owned missing-override control")
    assert not failures, "; ".join(failures)


@pytest.mark.parametrize("before", [None, ""])  # no Latet yet; one with neither
def test_install_ps1_goes_on_without_names_and_images_where_they_cannot_be_installed(ps_stand, before):
    stand = ps_stand()
    if before is not None:
        stand.installed(before)
    stand.fail(f"latet[ner,ocr] @ {WHEEL}")
    done = _run_ps(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}", f"latet @ {WHEEL}"]
    assert "without them" in done.stdout and "latet ner install" not in done.stdout
    assert done.stdout.rstrip().splitlines()[-1] == OFF  # the run ends saying what is off


@pytest.mark.parametrize("extras", ["ner, ocr", "ner", "ocr"])
def test_install_ps1_never_takes_away_names_or_images_an_installed_latet_has(ps_stand, extras):
    stand = ps_stand()
    stand.installed(extras)
    stand.fail(f"latet[ner,ocr] @ {WHEEL}")
    done = _run_ps(stand)
    assert done.returncode != 0 and "left as it was" in done.stdout + done.stderr, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]


def test_install_ps1_with_a_uv_too_old_to_say_keeps_an_latet_as_it_was(ps_stand):
    stand = ps_stand()
    stand.old_uv()
    _write(stand.bin / "latet.ps1", f"Add-Content '{stand.log}' \"latet $args\"\n")  # one is installed
    stand.fail(f"latet[ner,ocr] @ {WHEEL}")
    done = _run_ps(stand)
    assert done.returncode != 0 and "left as it was" in done.stdout + done.stderr, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]


@pytest.mark.parametrize("said", ["latet stop", "uv tool update-shell"])
@pytest.mark.parametrize("shell", SHELLS or [None], ids=lambda shell: Path(shell).stem if shell else "none")
def test_install_ps1_goes_on_when_a_command_it_runs_quietly_writes_to_stderr(ps_stand, shell, said):
    """A command the installer runs quietly may write to stderr: uv's PATH update does, when its folder is on PATH
    already. Windows PowerShell 5.1 makes a program's stderr sent to $null an error, which "Stop" turns into the end
    of the install: here it is none. The stub of `latet stop` stands for any command that writes to stderr."""
    if shell is None:
        pytest.skip("install.ps1 runs on Windows, with PowerShell")
    stand = ps_stand()
    if said == "latet stop":  # a Latet is installed; its stub writes to stderr (the real `latet stop` does not)
        logged = f"Add-Content '{stand.log}' \"latet $args\""
        _write(stand.bin / "latet.ps1", f'{logged}\n& cmd.exe /c "echo a warning on stderr 1>&2"\n')
    modify = {"LATET_NO_MODIFY_PATH": "1" if said == "latet stop" else ""}  # empty: PATH updated, by default
    done = _run_ps(stand, shell, **modify)
    assert done.returncode == 0, done.stdout + done.stderr
    assert said in stand.calls() and stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]
    assert "latet ner install" in done.stdout  # it went to its end


@pytest.mark.parametrize(
    ("release", "why"),
    [(None, "there is no release of Latet yet"), (ELSEWHERE, "the latest release has no wheel of Latet")],
)
def test_install_ps1_with_no_release_installs_nothing(ps_stand, release, why):
    stand = ps_stand(release)
    _looked_up_nothing(_run_ps(stand), why)
    assert stand.installs() == []


def test_install_ps1_says_why_github_did_not_answer(ps_stand):
    stand = ps_stand()
    stand.answers(403)  # and the page of the latest release names none
    _looked_up_nothing(_run_ps(stand), LIMIT)
    stand.offline.write_text("")
    _looked_up_nothing(_run_ps(stand), "GitHub could not be reached (No such host is known.)")
    assert stand.installs() == []


def test_install_ps1_reads_the_page_of_the_latest_release_where_the_api_does_not_answer(ps_stand):
    stand = ps_stand()
    stand.answers(403)
    stand.redirects(f"{PAGE}/tag/v1.2.0")
    done = _run_ps(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.installs() == [f"latet[ner,ocr] @ {WHEEL}"]
    assert f"{LIMIT}: the latest release is read from its page instead." in done.stdout
    assert stand.calls()[1] == f"iwr {PAGE}/latest"


def test_install_ps1_builds_latet_again_from_what_latet_source_names(ps_stand):
    """A clone keeps its version while its code changes: uv would keep the build it installed before."""
    stand = ps_stand()
    done = _run_ps(stand, LATET_SOURCE="latet[ner,ocr] @ file:///C:/work/latet")
    assert done.returncode == 0, done.stdout + done.stderr
    assert all("--reinstall-package latet" in line for line in stand.install_arguments()), stand.install_arguments()


def test_install_ps1_takes_a_release_wheel_as_it_is(ps_stand):
    stand = ps_stand()
    done = _run_ps(stand)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.install_arguments() and not any("--reinstall" in line for line in stand.install_arguments())


def test_install_ps1_installs_what_latet_source_names_and_asks_github_nothing(ps_stand):
    stand = ps_stand()
    done = _run_ps(stand, LATET_SOURCE="dist\\latet-1.2.0-py3-none-any.whl")
    assert done.returncode == 0, done.stdout + done.stderr
    assert stand.calls() == ["uv tool install dist\\latet-1.2.0-py3-none-any.whl"]
    assert "latet ner install" not in done.stdout


@pytest.mark.parametrize(
    ("source", "named"),
    [
        ("latet[ner,ocr] @ file:///C:/work/latet", ["latet ner install", "latet ocr install"]),
        ("latet[ner] @ file:///C:/work/latet", ["latet ner install"]),
        ("latet[ocr] @ file:///C:/work/latet", ["latet ocr install"]),
    ],
    ids=["both", "names", "images"],
)
def test_install_ps1_recommends_the_models_of_the_extras_latet_source_names(ps_stand, source, named):
    stand = ps_stand()
    done = _run_ps(stand, LATET_SOURCE=source)
    assert done.returncode == 0, done.stdout + done.stderr
    for command in ("latet ner install", "latet ocr install"):
        assert (command in done.stdout) == (command in named), done.stdout
