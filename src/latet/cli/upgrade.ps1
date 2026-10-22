# `latet upgrade` on Windows, in a window of its own. Windows replaces no file of a program that runs, and the command
# runs from the Latet it replaces: run from there, uv could not remove that Latet's folder and left it half gone. The
# command copies this script out of that folder, starts it and ends; the script waits for the command and its launcher
# to end, installs the release with uv, then has the new version apply the integrations and start Latet. Where uv
# cannot install it, the Latet that was there starts again. Windows PowerShell 5.1 runs it.
param(
    [string]$WaitFor,  # the process ids of the command and its launcher, comma-separated
    [string]$Uv,
    [string]$Pythons,
    [string]$Full,  # the release with name detection and the text in images
    [string]$Plain,  # the release without them
    [string]$Had,  # 1: the Latet that runs has them, and is never upgraded without them
    [string]$Latet,
    [string]$Version,
    [switch]$Quiet  # the tests': no wait before the window closes
)

foreach ($id in ($WaitFor -split ',')) {
    if ($id -match '^\d+$') {
        Wait-Process -Id ([int]$id) -Timeout 60 -ErrorAction SilentlyContinue
    }
}

Write-Host "Installing Latet $Version..."
& $Uv tool install --upgrade --python $Pythons $Full
$installed = $LASTEXITCODE -eq 0
$off = $false  # installed without name detection and the text in images
if (-not $installed -and $Had -ne '1') {
    Write-Host 'Name detection and the text in images cannot be installed here: installing Latet without them.'
    & $Uv tool install --upgrade --python $Pythons $Plain
    $installed = $LASTEXITCODE -eq 0
    $off = $installed
}

if ($installed) {
    & $Latet agents refresh  # the new version applies the integrations: the new plugin, the same choices
    & $Latet start
    Write-Host "Latet $Version installed."
    if ($off) {
        Write-Host "Off: name detection and the text in images, which uv could not install here. 'latet upgrade' tries again."
    }
    if (-not $Quiet) {
        Write-Host 'This window closes in 30 seconds.'
        Start-Sleep -Seconds 30
    }
} else {
    & $Latet start
    if ($Had -eq '1') {
        Write-Host ('The upgrade failed: uv could not install it with name detection and the text in images, which ' +
            'this Latet has. Latet is as it was.')
    } else {
        Write-Host 'The upgrade failed: Latet is as it was.'
    }
    if (-not $Quiet) {
        Read-Host 'Press Enter to close this window'
    }
}
if (-not $Quiet) {
    Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue  # the copy the command made
}
