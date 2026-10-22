# Latet installed on a Windows computer with neither uv nor Python, as a person runs the installer there: it brings uv,
# uv brings a Python Latet is tested on, Latet installs with name detection and the text in images, starts, masks, and
# stops. CI's fresh-install job runs it in a PowerShell started with an empty home and the system's own folders alone on
# the PATH (so that $HOME is that home); the wheel to install is its argument.
param([Parameter(Mandatory)][string]$Wheel)
$ErrorActionPreference = 'Stop'

foreach ($found in 'uv', 'python', 'python3', 'py') {
    if (Get-Command $found -ErrorAction SilentlyContinue) { throw "$found is here already: this is not a computer without uv or Python." }
}
# Windows lists Pythons in its registry, where uv looks too: a fresh computer has none there.
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:LATET_NO_MODIFY_PATH = '1'
$env:UV_NO_MODIFY_PATH = '1'

& (Join-Path $PSScriptRoot 'install.ps1') -Source "latet[ner,ocr] @ file:///$($Wheel -replace '\\', '/')"

$latet = Join-Path $HOME '.local\bin\latet.exe'
& $latet --help | Out-Null
& $latet start
& $latet status
$doctor = & $latet doctor 2>&1 | Out-String
$doctor
if ($doctor -notmatch '(?m)^\[OK  \] Masking self-test') { throw 'The masking self-test did not pass.' }
if ($doctor -notmatch '(?m)^\[OK  \] Names: ') { throw 'Name detection is not installed.' }  # what it needs next: its model
& $latet stop
'Installed, started, masked and stopped on a computer that had neither uv nor Python.'
