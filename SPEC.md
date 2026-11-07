# recorder

Record a meeting's audio — both the other participants **and** your own voice — to a
single mp3, with two commands and **no echo**.

```
recorder start     # begin capturing in the background, returns immediately
recorder stop      # finish and save audio/recording-YYYYMMDD-HHMMSS.mp3
recorder status    # is a recording in progress?
recorder devices   # list the capture devices (debugging wrong-device silence)
```

## Why this exists

OBS can record a Teams call, but on a remote/devbox session its only way to also let you
*hear* yourself (monitoring) feeds your voice back into your ears — an echo that makes the
call unusable. A headless recorder never plays anything back, so **echo is impossible by
construction**. This reproduces what OBS captured (call output + microphone) without the
monitor path.

## What it captures

- **The call** — the default speaker's WASAPI **loopback** (everyone you hear). On the devbox
  that is the "Remote Audio" device.
- **Your voice** — the default **microphone**. Your own voice does not travel through your
  headphones, so it must be taken from the mic and mixed in.

Both streams are captured in **stereo** (WASAPI returns garbage for single-channel capture),
summed with clipping, and encoded straight to mp3.

## How start/stop talk to each other

Two separate process invocations, coordinated through a small state directory:

- `start` spawns a **detached, window-less** daemon (`python -m recorder._daemon`), records
  `.state/recording.json` (pid, start time, output path), waits ~1s to catch an immediate
  device failure, then returns.
- The daemon captures the loopback and mic on two threads, mixes block-by-block, and streams
  mp3 to `audio/recording-*.mp3.part`. It watches for `.state/stop.flag`.
- `stop` drops `.state/stop.flag`, waits for the daemon to flush and rename `.part` → `.mp3`,
  then prints the saved path. If the daemon dies or has to be killed first, `stop` renames the
  leftover `.part` itself — mp3 frames are self-contained, so a cut-off file still plays.

The daemon publishes the mp3 *before* tearing PortAudio down, and its readers poll
`get_read_available()` instead of blocking in `read()`: a WASAPI loopback delivers nothing while
the call is silent, and closing a device another thread is still blocked in crashes the process.

Stop is signalled with a **sentinel file**, not a POSIX signal, because signals are
unreliable on Windows.

## Layout

```
recorder/
├─ pyproject.toml          # exposes the `recorder` command
├─ SPEC.md
├─ src/recorder/
│  ├─ config.py            # capture constants (samplerate, blocksize, bitrate)
│  ├─ state.py             # paths, state file, pid liveness, stop flag, log tail
│  ├─ mix.py               # pure stereo mix (the unit-tested core)
│  ├─ engine.py            # device selection + threaded capture/mix/encode loop
│  ├─ _daemon.py           # detached background entry point
│  └─ cli.py               # argparse subcommands
├─ tests/                  # mix, state lifecycle, cli dispatch
└─ audio/                  # finished mp3s (gitignored)
```

## Dependencies

`soundcard` (WASAPI loopback + mic), `numpy` (mixing), `lameenc` (mp3, no ffmpeg needed).

## Known limits

- Mic and loopback are clocked by separate devices, so a very long recording can drift. The
  block-for-block pairing keeps them locked step-wise; if drift ever bites, split to stereo
  (mic left / call right) instead of summing.
- Designed for an editable install (`pip install -e`); paths resolve relative to this folder,
  overridable with `RECORDER_HOME`.
