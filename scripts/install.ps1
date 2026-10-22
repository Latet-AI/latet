<#
.SYNOPSIS
    Install Latet on Windows: uv if missing, then Latet as an isolated tool. Nothing else changes.

.DESCRIPTION
    irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex

    Nothing here needs administrator rights, and none of your agents' settings change: start an agent
    through Latet with `latet claude` (or `latet codex`, `latet opencode`), and route your
    agents for good with `latet global-install` once it fits your work. Latet lives in uv's tool
    directory and is removed with `latet global-uninstall` followed by `uv tool uninstall latet`.

    It installs the latest release from GitHub, with local name detection and the text in images
    (without them where they cannot be installed, unless the Latet installed here has them: it is
    then left as it was); their models are downloaded only when you ask.

.PARAMETER Source
    What to install instead: a wheel file, or a requirement such as "latet @ <url>".

.NOTES
    LATET_SOURCE and LATET_NO_MODIFY_PATH=1 (leave PATH alone) do the same from the environment,
    for `irm | iex` where parameters cannot be passed.
#>
param(
    [string]$Source = $env:LATET_SOURCE
)

$ErrorActionPreference = "Stop"
$Repository = "Latet-AI/latet"
$Page = "https://github.com/$Repository/releases"

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
        if (Test-Path "$dir\uv.exe") { return "$dir\uv.exe" }
    }
    return $null
}

# Whether the Latet installed here has local name detection or the text in images: installed again without
# them, it would lose them. A uv too old to say counts as yes, when there is a Latet.
function Test-Extras {
    $ErrorActionPreference = "Continue"  # uv's warnings on stderr are no error here
    $listed = & $uv tool list --show-extras 2>$null
    if ($LASTEXITCODE -ne 0) { return [bool]$existing }
    return [bool]($listed | Where-Object { "$_" -match '^latet .*\[extras: .*(ner|ocr)' })
}

# Runs a command for what it does: what it says is not needed, on stderr either. Windows PowerShell 5.1, where
# `irm | iex` runs, takes a program's stderr sent to $null for an error, which "Stop" would make the end of the install.
function Invoke-Quietly([string]$Command) {
    $ErrorActionPreference = "Continue"
    & $Command @args 2>$null | Out-Null
}

# The tag of the latest release, from its page, which GitHub redirects to the release: not the API, so not its
# limit of 60 requests an hour from one address.
function Find-Tag {
    try {
        $answer = Invoke-WebRequest "$Page/latest" -UseBasicParsing
    } catch {
        return $null
    }
    $base = $answer.BaseResponse
    $uri = if ($base.ResponseUri) { $base.ResponseUri } else { $base.RequestMessage.RequestUri }
    if ("$uri" -match "^$([regex]::Escape($Page))/tag/(v[0-9][0-9A-Za-z.+-]*)$") { return $Matches[1] }
    return $null
}

# The wheel of the latest release, from Latet's own releases only: it carries the dashboard, which the
# repository's source does not. From GitHub's API, or, where the API does not answer, from the page of the latest
# release: its wheel is named after its tag, as the release workflow checks. Throws why there is none.
function Find-Wheel {
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/$Repository/releases/latest"
    } catch {
        $response = $_.Exception.Response
        $code = if ($response -and $response.StatusCode) { [int]$response.StatusCode } else { 0 }
        if ($code -eq 404) { throw "there is no release of Latet yet" }
        $why = if ($code -eq 0) { "GitHub could not be reached ($($_.Exception.Message))" } else { "GitHub's API answered $code" }
        if ($code -eq 403 -or $code -eq 429) { $why += " (it takes 60 requests an hour from one address)" }
        $tag = Find-Tag
        if (-not $tag) { throw $why }
        Write-Host "$($why): the latest release is read from its page instead."
        return "$Page/download/$tag/latet-$($tag.Substring(1))-py3-none-any.whl"
    }
    $own = "$Page/download/"
    $wheel = $release.assets | Where-Object {
        "$($_.name)".EndsWith(".whl") -and "$($_.browser_download_url)".StartsWith($own)
    } | Select-Object -First 1
    if (-not $wheel) { throw "the latest release has no wheel of Latet" }
    return $wheel.browser_download_url
}

# The extras a source names, as a clone installed so does: their models are the next step.
$ner = [bool]$Source -and $Source -match '\[[^\]]*\bner\b'
$ocr = [bool]$Source -and $Source -match '\[[^\]]*\bocr\b'
if ($Source) {
    $sources = @($Source)
} else {
    try {
        $wheel = Find-Wheel
    } catch {
        throw ("Could not look up the latest release of Latet: $($_.Exception.Message). The releases are at " +
            "$Page. To install one by hand, set LATET_SOURCE to its wheel (`"latet[ner,ocr] @ <its address>`", " +
            "or a file you downloaded) and run this again.")
    }
    $sources = @("latet[ner,ocr] @ $wheel", "latet @ $wheel")
}

$uv = Find-Uv
if (-not $uv) {
    Write-Host "Installing uv, the Python tool manager Latet is installed with (https://docs.astral.sh/uv/)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uv = Find-Uv
    if (-not $uv) { throw "uv was installed but cannot be found: open a new terminal and run this again." }
}

$existing = Get-Command latet -ErrorAction SilentlyContinue
# True: the Latet installed here has name detection or the text in images.
$had = $sources.Count -gt 1 -and (Test-Extras)

# If Latet is already running, stop it first: Windows cannot replace files that are in use.
if ($existing) {
    Invoke-Quietly $existing.Source stop
}

Write-Host "Installing Latet ($($sources[0]))..."
# The Pythons Latet is tested on. uv takes the newest Python it finds or can download, and on a newer one Latet's
# dependencies may have no wheel yet (google-re2 then has to be built, and cannot be).
$pythons = ">=3.12,<3.14"
# A clone keeps its version while its code changes: what LATET_SOURCE names is built again, every time.
$rebuild = if ($Source) { @("--reinstall-package", "latet") } else { @() }
& $uv tool install --upgrade --python $pythons @rebuild $sources[0]
# True: name detection and the text in images are in, their models are the next step.
$models = $LASTEXITCODE -eq 0 -and ($sources.Count -gt 1 -or ($ner -and $ocr))
$off = $false  # True: installed without them, as uv could not install them here
if ($LASTEXITCODE -ne 0 -and $had) {
    throw ("uv could not install the new release with local name detection and the text in images, which your " +
        "Latet has: it is left as it was ('latet start' starts it again). Run this again later.")
}
if ($LASTEXITCODE -ne 0 -and $sources.Count -gt 1) {
    Write-Host "Local name detection and the text in images cannot be installed here: installing Latet without them..."
    & $uv tool install --upgrade --python $pythons $sources[1]
    $off = $LASTEXITCODE -eq 0
}
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit code $LASTEXITCODE)." }
if (-not $env:LATET_NO_MODIFY_PATH) { Invoke-Quietly $uv tool update-shell }

$bin = (& $uv tool dir --bin).Trim()
$latet = Join-Path $bin "latet.exe"
if (-not (Test-Path $latet)) { throw "Latet was installed but $latet is missing." }
if (-not (($env:Path -split ";") -contains $bin)) { $env:Path = "$bin;$env:Path" }

& $latet version
Write-Host ""
Write-Host "Installed. New to Latet? 'latet tutorial' shows it at work in about 30 minutes."
Write-Host "Start your agent through Latet: 'latet claude' (or 'latet codex', 'latet opencode')."
Write-Host "When it fits your work, 'latet global-install' routes your agents through Latet for good."
if ($models) {
    Write-Host "Recommended from day one: 'latet ner install' and 'latet ocr install' download the models that find"
    Write-Host "names and read the text in images (about 1.6 GB and 31 MB, once). They run on this computer only."
} elseif ($off) {
    Write-Host "Off: local name detection and the text in images, which uv could not install here. 'latet upgrade' tries again."
} elseif ($ner) {
    Write-Host "Recommended from day one: 'latet ner install' downloads the model that finds names (about 1.6 GB, once)."
    Write-Host "It runs on this computer only."
} elseif ($ocr) {
    Write-Host "Recommended from day one: 'latet ocr install' downloads the models that read the text in images (about"
    Write-Host "31 MB, once). They run on this computer only."
}
