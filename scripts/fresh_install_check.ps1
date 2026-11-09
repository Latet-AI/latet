# Latet installed on a Windows computer with neither uv nor Python, as a person runs the installer there: it brings uv,
# uv brings a Python Latet is tested on, Latet installs with name detection and the text in images, starts, masks, and
# stops. CI's fresh-install job runs it in a PowerShell started with an empty home and the system's own folders alone on
# the PATH (so that $HOME is that home); the wheel to install is its argument.
param([Parameter(Mandatory)][string]$Wheel)
$ErrorActionPreference = 'Stop'

# Not `py`: GitHub's runner keeps its launcher in C:\Windows, which the PATH needs; uv takes its own Python here.
foreach ($found in 'uv', 'python', 'python3') {
    if (Get-Command $found -ErrorAction SilentlyContinue) { throw "$found is here already: this is not a computer without uv or Python." }
}
# Windows lists Pythons in its registry, where uv looks too: a fresh computer has none there.
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:LATET_NO_MODIFY_PATH = '1'
$env:UV_NO_MODIFY_PATH = '1'

& (Join-Path $PSScriptRoot 'install.ps1') -Source "latet[ner,ocr] @ file:///$($Wheel -replace '\\', '/')"

$latet = Join-Path $HOME '.local\bin\latet.exe'
& $latet --help | Out-Null
# The Python uv brought where there was none: the newest the installer asks for, the one below $pythons' bound.
$bound = Select-String -Path (Join-Path $PSScriptRoot 'install.ps1') -Pattern '^\$pythons = ">=3\.\d+,<3\.(\d+)"$'
if (-not $bound) { throw 'install.ps1 names no bound of the Pythons it asks for.' }
$newest = "3.$([int]$bound.Matches[0].Groups[1].Value - 1)"
$listed = & (Join-Path $HOME '.local\bin\uv.exe') tool list --show-python | Select-String '^latet v\S+ \[CPython (3\.\d+)\.\d+\]$'
$brought = if ($listed) { $listed.Matches[0].Groups[1].Value } else { 'unnamed by uv' }
if ($brought -ne $newest) { throw "Latet runs on Python $brought, not $newest, the newest the installer asks for." }
"Latet runs on Python $brought, the newest the installer asks for."
& $latet start
if ($LASTEXITCODE -ne 0) {
    Get-ChildItem $HOME -Recurse -Include daemon.log, daemon.out -ErrorAction SilentlyContinue | ForEach-Object { "== $_"; Get-Content $_ -Tail 60 }
    throw 'Latet did not start.'
}
& $latet status
$doctor = & $latet doctor 2>&1 | Out-String
$doctor
if ($doctor -notmatch '(?m)^\[OK  \] Masking self-test') { throw 'The masking self-test did not pass.' }
if ($doctor -notmatch '(?m)^\[OK  \] Names: ') { throw 'Name detection is not installed.' }  # what it needs next: its model
& $latet stop
'Installed, started, masked and stopped on a computer that had neither uv nor Python.'
