# Demo recording

The GIF embedded in the top-level `README.md` is not hand-recorded. It is
rendered from a scripted session so it can be reproduced whenever the CLI's
output changes, instead of quietly drifting out of date the way a one-off
screen capture does.

- `demo.sh` — the content of the recording: the commands, the pacing, and the
  short prompt it prints instead of whoever's real shell prompt. Run it
  directly to iterate on the script without recording anything.
- `record.sh` — wraps `demo.sh` in `asciinema`, renders the resulting cast to
  `demo.gif` with `agg`, and installs both tools into `~/.local/bin` if they
  are missing. Neither needs root.

```sh
docs/demo/record.sh
cp docs/demo/demo.gif docs/images/demo.gif
```

`asciinema` needs a real TTY, so this cannot run from CI or a non-interactive
session. Size the terminal to roughly 100x30 first: `asciinema` 2.x takes the
frame size from the terminal it runs in (3.x is passed `--cols`/`--rows`).

## What the recording has to show

Two things are the point of the demo and should survive any edit:

- **The wire trace.** The strongest claim this project makes is that an
  assertion only ever observes telemetry by decoding a frame that crossed the
  bus. `--trace` is what makes that visible, including the OBC's own
  low-battery command as bytes.
- **A deliberate failure with a non-zero exit code.** A demo where everything
  passes says nothing about whether the tool would catch a regression. The
  script breaks the injected battery level so the FDIR rule never trips, and
  the assertion fails with the observed value — which is exactly what CI sees.

## What it installs

`demo.sh` installs the published `cubesat-testbed` package, which is what a
viewer of the recording would type. To demonstrate something that has landed on
`main` but is not released yet, override `INSTALL_SPEC`:

```sh
INSTALL_SPEC=git+https://github.com/glocker/CubeSatTestbed@main docs/demo/record.sh
```

The GIF currently committed was recorded that way, just before the 1.1.0
release, and shows the git URL. Re-recording it against PyPI is worth doing the
next time the output changes.
