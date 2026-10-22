#!/bin/sh
# Latet installed on a computer with neither uv nor Python, as a person runs the installer there: it brings uv, uv
# brings a Python Latet is tested on, Latet installs with name detection and the text in images, starts, masks, and
# stops. CI's fresh-install job runs it in clean Linux containers and on macOS with the system's own folders alone on
# the PATH and an empty home; it needs sh and curl, and the wheel to install as its argument.
set -eu

wheel="$1"
for found in uv python3.12 python3.13 python3.14; do
    if command -v "$found" >/dev/null 2>&1; then
        echo "$found is here already: this is not a computer without uv or Python." >&2
        exit 1
    fi
done
for found in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do  # where the installer looks for uv besides the PATH
    if [ -e "$found" ]; then
        echo "$found is here already: this is not a computer without uv." >&2
        exit 1
    fi
done

LATET_SOURCE="latet[ner,ocr] @ file://$wheel" LATET_NO_MODIFY_PATH=1 UV_NO_MODIFY_PATH=1 \
    sh "$(dirname "$0")/install.sh"

PATH="$HOME/.local/bin:$PATH"
export PATH
latet --help >/dev/null
if ! latet start; then
    find "$HOME" \( -name daemon.log -o -name daemon.out \) -type f 2>/dev/null | while read -r log; do
        echo "== $log"
        tail -n 60 "$log"
    done
    exit 1
fi
latet status
latet doctor >"$HOME/doctor.txt" 2>&1 || true
cat "$HOME/doctor.txt"
grep -q '^\[OK  \] Masking self-test' "$HOME/doctor.txt"
grep -q '^\[OK  \] Names: ' "$HOME/doctor.txt"  # the ner extra installed: what it needs next is its model
if grep -q '^\[WARN\] Data folder' "$HOME/doctor.txt"; then  # Ubuntu's `~/.local` is 0775, its group the person's own
    echo "doctor warns of a data folder on a computer as it comes" >&2
    exit 1
fi
latet stop
echo "Installed, started, masked and stopped on a computer that had neither uv nor Python."
