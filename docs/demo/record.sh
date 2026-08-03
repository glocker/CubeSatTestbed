#!/usr/bin/env bash
# Record demo.sh as an asciinema cast and render it to an animated GIF.
#
# Installs both tools into ~/.local/bin without root: asciinema is pure
# Python, agg is a single static binary. Run this from a real terminal --
# asciinema needs a TTY, which is the one thing a non-interactive session
# cannot give it.
set -euo pipefail

AGG_VERSION="${AGG_VERSION:-v1.9.0}"

# agg ships one binary per architecture and there is no universal build, so
# pick by uname rather than assuming x86_64 -- guessing wrong fails late, at
# render time, with a bare "Exec format error" after the cast is recorded.
if [ -z "${AGG_TARGET:-}" ]; then
    case "$(uname -m)" in
        x86_64 | amd64) AGG_TARGET="x86_64-unknown-linux-musl" ;;
        aarch64 | arm64) AGG_TARGET="aarch64-unknown-linux-gnu" ;;
        armv7l | armv6l) AGG_TARGET="arm-unknown-linux-gnueabihf" ;;
        *)
            echo "no agg build for $(uname -m); set AGG_TARGET manually" >&2
            exit 1
            ;;
    esac
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-$HERE}"
CAST="$OUT_DIR/demo.cast"
GIF="$OUT_DIR/demo.gif"

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR" "$OUT_DIR"
export PATH="$BIN_DIR:$PATH"

if ! command -v asciinema >/dev/null 2>&1; then
    echo "installing asciinema..."
    if command -v uv >/dev/null 2>&1; then
        uv tool install asciinema
    else
        python3 -m pip install --user asciinema
    fi
fi

if ! command -v agg >/dev/null 2>&1 || ! agg --version >/dev/null 2>&1; then
    echo "installing agg $AGG_VERSION for $AGG_TARGET..."
    curl -fsSL -o "$BIN_DIR/agg" \
        "https://github.com/asciinema/agg/releases/download/$AGG_VERSION/agg-$AGG_TARGET"
    chmod +x "$BIN_DIR/agg"
    # Fail here, not after a recording, if the binary still does not run.
    agg --version >/dev/null
fi

rm -f "$CAST" "$GIF"

# --cols/--rows exist in asciinema 3.x but not 2.x, where the pty simply
# inherits the terminal. Probe instead of pinning a version: on 2.x, size
# your terminal to about 100x30 before recording.
REC_ARGS=(--idle-time-limit 1 --command "bash $HERE/demo.sh")
if asciinema rec --help 2>&1 | grep -q -- '--cols'; then
    REC_ARGS+=(--cols 100 --rows 30)
fi

echo "recording -> $CAST"
asciinema rec "${REC_ARGS[@]}" "$CAST"

echo "rendering -> $GIF"
agg --font-size 16 --speed 1.3 --theme asciinema "$CAST" "$GIF"

ls -lh "$GIF"
echo
echo "Preview it, then copy into the repo:"
echo "  cp $GIF ~/CubesatTestbed/docs/images/demo.gif"
