#!/bin/sh
# Install Latet on macOS or Linux: uv if missing, then Latet as an isolated tool. Nothing else changes.
#
#   curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | sh
#
# Nothing here needs root, and none of your agents' settings change: start an agent through Latet
# with `latet claude` (or codex, opencode), and route your agents for good with
# `latet global-install` once it fits your work. Remove Latet with `latet global-uninstall`
# then `uv tool uninstall latet`.
# It installs the latest release from GitHub, with local name detection and the text in images (without
# them where they cannot be installed, unless the Latet installed here has them: it is then left as it
# was); their models are downloaded only when you ask.
# LATET_SOURCE: what to install instead (a wheel file, or a requirement such as "latet @ <url>").
# LATET_NO_MODIFY_PATH=1: leave the shell profile alone.
set -eu

REPOSITORY="Latet-AI/latet"
PAGE="https://github.com/$REPOSITORY/releases"

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        echo "Neither curl nor wget is available: install one of them first." >&2
        return 1
    fi
}

# GET $1 into $TMP/body, and print the HTTP status ("000": no answer).
http_get() {
    if command -v curl >/dev/null 2>&1; then
        curl -sSL -o "$TMP/body" -w '%{http_code}' "$1" 2>/dev/null || true
    else
        wget -q -S -O "$TMP/body" "$1" 2>"$TMP/headers" || true
        CODE="$(sed -n 's|^ *HTTP/[0-9.]* \([0-9][0-9][0-9]\).*|\1|p' "$TMP/headers" | tail -n 1)"
        echo "${CODE:-000}"
    fi
}

# The tag of the latest release, from its page, which GitHub redirects to the release: not the API, so not its
# limit of 60 requests an hour from one address.
latest_tag() {
    if command -v curl >/dev/null 2>&1; then
        curl -sS -I "$PAGE/latest" 2>/dev/null || true
    else
        wget -q -S --max-redirect=0 -O /dev/null "$PAGE/latest" 2>&1 || true
    fi | tr -d '\r' |
        sed -n "s|^ *[Ll]ocation: *https://github\.com/$REPOSITORY/releases/tag/\(v[0-9][0-9A-Za-z.+-]*\)\$|\1|p" |
        head -n 1
}

# The wheel of the latest release (WHEEL), or why there is none (WHY), from Latet's own releases only: it carries
# the dashboard, which the repository's source does not. From GitHub's API, or, where the API does not answer, from
# the page of the latest release: its wheel is named after its tag, as the release workflow checks.
find_wheel() {
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        WHY="neither curl nor wget is available: install one of them first"
        return 0
    fi
    STATUS="$(http_get "https://api.github.com/repos/$REPOSITORY/releases/latest")"
    case "$STATUS" in
        200)
            WHEEL="$(sed -n "s|.*\"browser_download_url\": *\"\(https://github\.com/$REPOSITORY/releases/download/[^\"]*\.whl\)\".*|\1|p" "$TMP/body" | head -n 1)"
            [ -n "$WHEEL" ] || WHY="the latest release has no wheel of Latet"
            return 0
            ;;
        404) WHY="there is no release of Latet yet"; return 0 ;;
        000) WHY="GitHub could not be reached (no connection, or a proxy in the way)" ;;
        403 | 429) WHY="GitHub's API answered $STATUS (it takes 60 requests an hour from one address)" ;;
        *) WHY="GitHub's API answered $STATUS" ;;
    esac
    TAG="$(latest_tag)"
    if [ -n "$TAG" ]; then
        echo "$WHY: the latest release is read from its page instead."
        WHEEL="$PAGE/download/$TAG/latet-${TAG#v}-py3-none-any.whl"
    fi
}

# Whether the Latet installed here has local name detection or the text in images: installed again without
# them, it would lose them. A uv too old to say counts as yes, when there is a Latet.
had_extras() {
    if LISTED="$("$UV" tool list --show-extras 2>/dev/null)"; then
        printf '%s\n' "$LISTED" | grep -Eq '^latet .*\[extras: .*(ner|ocr)'
    else
        command -v latet >/dev/null 2>&1
    fi
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for dir in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -x "$dir/uv" ]; then
            echo "$dir/uv"
            return 0
        fi
    done
    return 1
}

NER=""  # set: local name detection is in, its model the next step
OCR=""  # set: the text in images is in, its models the next step
if [ -n "${LATET_SOURCE:-}" ]; then
    SOURCE="$LATET_SOURCE"
    PLAIN=""
    case "$SOURCE" in *\[*ner*\]*) NER=1 ;; esac  # the extras a source names, as a clone installed so does
    case "$SOURCE" in *\[*ocr*\]*) OCR=1 ;; esac
else
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    WHEEL=""
    WHY=""
    find_wheel
    if [ -z "$WHEEL" ]; then
        echo "Could not look up the latest release of Latet: $WHY." >&2
        echo "The releases are at $PAGE. To install one by hand, set LATET_SOURCE to its wheel" >&2
        echo "(\"latet[ner,ocr] @ <its address>\", or a file you downloaded) and run this again." >&2
        exit 1
    fi
    SOURCE="latet[ner,ocr] @ $WHEEL"
    PLAIN="latet @ $WHEEL"
fi

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
    echo "Installing uv, the Python tool manager Latet is installed with (https://docs.astral.sh/uv/)..."
    fetch https://astral.sh/uv/install.sh | sh
    UV="$(find_uv || true)"
    if [ -z "$UV" ]; then
        echo "uv was installed but cannot be found: open a new terminal and run this again." >&2
        exit 1
    fi
fi

HAD=""  # set: the Latet installed here has name detection or the text in images
if [ -n "$PLAIN" ] && had_extras; then
    HAD=1
fi

if command -v latet >/dev/null 2>&1; then
    latet stop >/dev/null 2>&1 || true  # restart cleanly on the new version
fi

MODELS=""  # set: name detection and the text in images are in, their models are the next step
OFF=""  # set: installed without them, as uv could not install them here
# The Pythons Latet is tested on. uv takes the newest Python it finds or can download, and on a newer one Latet's
# dependencies may have no wheel yet (google-re2 then has to be built, and cannot be).
PYTHONS=">=3.12,<3.14"
echo "Installing Latet ($SOURCE)..."
if [ -n "${LATET_SOURCE:-}" ]; then
    set -- --reinstall-package latet  # a clone keeps its version while its code changes: built again, every time
else
    set --
fi
if "$UV" tool install --upgrade --python "$PYTHONS" "$@" "$SOURCE"; then
    if [ -n "$PLAIN" ] || { [ -n "$NER" ] && [ -n "$OCR" ]; }; then
        MODELS=1
    fi
elif [ -n "$HAD" ]; then
    echo "uv could not install the new release with local name detection and the text in images, which your" >&2
    echo "Latet has: it is left as it was ('latet start' starts it again). Run this again later." >&2
    exit 1
elif [ -n "$PLAIN" ]; then
    echo "Local name detection and the text in images cannot be installed here: installing Latet without them..."
    "$UV" tool install --upgrade --python "$PYTHONS" "$PLAIN"
    OFF=1
else
    exit 1
fi
if [ -z "${LATET_NO_MODIFY_PATH:-}" ]; then
    "$UV" tool update-shell >/dev/null 2>&1 || true
fi

BIN="$("$UV" tool dir --bin)"
LATET="$BIN/latet"
if [ ! -x "$LATET" ]; then
    echo "Latet was installed but $LATET is missing." >&2
    exit 1
fi

"$LATET" version
echo
if ! command -v latet >/dev/null 2>&1; then  # uv's folder is on the PATH of the terminals opened next
    if [ -z "${LATET_NO_MODIFY_PATH:-}" ]; then
        echo "Open a new terminal first: the terminals you open next find 'latet', this one does not yet."
    else
        echo "'latet' is not on your PATH: add $BIN to it, or call $LATET."
    fi
fi
echo "Installed. New to Latet? 'latet tutorial' shows it at work in about 30 minutes."
echo "Start your agent through Latet: 'latet claude' (or 'latet codex', 'latet opencode')."
echo "When it fits your work, 'latet global-install' routes your agents through Latet for good."
if [ -n "$MODELS" ]; then
    echo "Recommended from day one: 'latet ner install' and 'latet ocr install' download the models that find"
    echo "names and read the text in images (about 1.6 GB and 31 MB, once). They run on this computer only."
elif [ -n "$OFF" ]; then
    echo "Off: local name detection and the text in images, which uv could not install here. 'latet upgrade' tries again."
elif [ -n "$NER" ]; then
    echo "Recommended from day one: 'latet ner install' downloads the model that finds names (about 1.6 GB, once)."
    echo "It runs on this computer only."
elif [ -n "$OCR" ]; then
    echo "Recommended from day one: 'latet ocr install' downloads the models that read the text in images (about"
    echo "31 MB, once). They run on this computer only."
fi
