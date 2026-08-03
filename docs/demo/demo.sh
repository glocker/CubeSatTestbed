#!/usr/bin/env bash
# Scripted CubeSatTestbed demo -- the *content* of the recording.
#
# Run it through record.sh, which wraps it in asciinema and renders the GIF.
# Running it directly also works and is the fastest way to iterate on the
# script without recording anything.
#
# Deliberately not `set -e`: the last step is *supposed* to fail, and its
# non-zero exit code is the point of the demo.
set -u

# Every command below exists in 1.1.0, so the demo installs the published
# package. Point this at git+https://github.com/glocker/CubeSatTestbed@main to
# record something that has landed on main but is not released yet.
INSTALL_SPEC="${INSTALL_SPEC:-cubesat-testbed}"

TYPE_DELAY="${TYPE_DELAY:-0.03}"   # seconds per typed character
PAUSE="${PAUSE:-1.2}"              # pause after a command's output

# A short prompt of our own: the recording must not show whatever host and
# username happen to be running it.
PROMPT_COLOR=$'\033[38;5;114m'
COMMENT_COLOR=$'\033[38;5;245m'
RESET=$'\033[0m'

say() {
    printf '%s# %s%s\n' "$COMMENT_COLOR" "$*" "$RESET"
    sleep 0.9
}

type_cmd() {
    printf '%s$%s ' "$PROMPT_COLOR" "$RESET"
    local i
    for ((i = 0; i < ${#1}; i++)); do
        printf '%s' "${1:i:1}"
        sleep "$TYPE_DELAY"
    done
    printf '\n'
    sleep 0.35
}

run() {
    type_cmd "$1"
    eval "$1"
    sleep "$PAUSE"
}

WORKDIR="$(mktemp -d)"
cd "$WORKDIR"

say "A CubeSat subsystem testbed: deterministic scenarios over a real CSP/CAN bus."
say "Install it into a clean virtualenv."
run "python3 -m venv .venv && . .venv/bin/activate && pip install -q $INSTALL_SPEC"
run "cubesat-testbed init --list"

say "Copy a packaged example out and run it: setup, scenario, PASS, exit code."
run "cubesat-testbed init demo && cd demo"
run "cubesat-testbed run -c setup.toml -s scenario.yaml; echo exit=\$?"

say "Assertions never read Python state -- they decode frames off the bus."
say "--trace prints every frame that actually crossed it."
run "cubesat-testbed run --example default --trace 2>&1 | head -8"

say "And the OBC's low-battery decision is on that same bus, as real bytes:"
run "cubesat-testbed run --example default --trace 2>&1 | grep command | head -2"

say "Subsystem behaviour lives in modules. Here a thermal node and an OBC"
say "rule close a heater control loop entirely over that same bus."
run "cubesat-testbed run --example thermal-heater"

say "Now break it on purpose: inject a battery level that never trips the rule."
say "The FDIR logic then never sheds the payload -- and the test must catch that."
run "sed -i s/value:\\ 25/value:\\ 95/ scenario.yaml"
run "cubesat-testbed run -c setup.toml -s scenario.yaml; echo exit=\$?"

say "FAIL, with the observed value, and exit code 1. That is what CI sees."
sleep 2
