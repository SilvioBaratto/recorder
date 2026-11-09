# recorder

Record a meeting's audio — both the other participants **and** your own voice — to a
single mp3, with two commands and **no echo**. Runs on Windows, macOS and Linux.

```
recorder start     # begin capturing in the background, returns immediately
recorder stop      # finish and save audio/recording-YYYYMMDD-HHMMSS.mp3
recorder status    # is a recording in progress?
recorder devices   # list the capture devices (debugging wrong-device silence)
```

Per-OS setup and troubleshooting are in `README.md`.

## Why this exists

OBS can record a Teams call, but on a remote/devbox session its only way to also let you
*hear* yourself (monitoring) feeds your voice back into your ears — an echo that makes the
call unusable. A headless recorder never plays anything back, so **echo is impossible by
construction**. This reproduces what OBS captured (call output + microphone) without the
monitor path.

## What it captures

- **The call** — everyone you hear: the audio your machine plays to its default output.
- **Your voice** — the default **microphone**. Your own voice does not travel through your
  headphones, so it must be taken from the mic and mixed in.

How each OS provides them:

| OS | Call audio | Microphone |
|----|------------|------------|
| Windows | WASAPI **loopback** of the default speaker, through PyAudioWPatch. On the devbox that is the "Remote Audio" device. | The default WASAPI input, through PyAudioWPatch. |
| macOS 14.2+ | A Core Audio **process tap** on the global output mix, through `catap`. By design it needs no virtual driver and no routing change, so headphones and the volume keys should be unaffected (not yet observed here). | The default input, through `sounddevice`. |
| Linux | `parec` on `<default sink>.monitor` (PulseAudio, or PipeWire through `pipewire-pulse`). | `parec` on the default source. A default source that is itself a `.monitor` is rejected, because it would record the call twice. |

Each source delivers blocks at its own native rate and channel count. Mono is duplicated to
stereo and channels beyond two are dropped; both streams are resampled to 48 kHz. Each
microphone block (4800 frames, about 0.1 s) is summed, with clipping, with whatever call audio
is buffered at that moment and encoded straight to a 128 kbps mp3 (`lameenc`, no ffmpeg
needed). The microphone sets the timeline: a silent or idle call side contributes silence and
never stalls the recording. On Linux the call side starts with a 0.3 s reserve so that `parec`
bursts arriving a moment late do not leave gaps (see Known limits).

## How start/stop talk to each other

Two separate process invocations, coordinated through a small state directory:

- `start` spawns a **detached, window-less** daemon (`python -m recorder._daemon`), records
  `.state/recording.json` (pid, start time, output path), waits ~1.2 s, and reports failure if
  the child has already exited (`proc.poll()`). Otherwise it returns.
- The daemon opens the platform backend's two sources, mixes block-by-block, and streams mp3 to
  `audio/recording-*.mp3.part`. It watches for `.state/stop.flag`.
- `stop` drops `.state/stop.flag`, waits for the daemon to flush and rename `.part` → `.mp3`,
  then prints the saved path. If the daemon dies or has to be terminated first, `stop` renames
  the leftover `.part` itself — mp3 frames are self-contained, so a cut-off file still plays.
- If the call side carried no signal for the whole recording, the daemon logs a `WARNING:`
  line to `.state/engine.log` and leaves the advice in `.state/warning.txt`; `stop` runs in a
  different process, so it prints that file as `Warning: ...`. Only macOS has such a hint (see
  Known limits).

The daemon publishes the mp3 *before* closing the audio sources and the backend. No source
can leave a thread parked inside a device read at teardown: Windows and the macOS microphone
go through `PolledSource`, which polls for available frames, because a WASAPI loopback
delivers nothing while the call is silent and closing a device another thread is still
blocked in crashes the process. The macOS tap is callback-driven, and a Linux `parec` pipe is
unblocked by terminating `parec`.

Stop is signalled with a **sentinel file**, not a POSIX signal, because signals are
unreliable on Windows. Process control is POSIX-aware where it has to be: `pid_alive` reaps an
exited child (a zombie still answers signal 0), and `terminate` sends `SIGTERM`, waits 2 s,
then `SIGKILL`, to the whole process group when the pid leads its own.

## Layout

```
recorder/
├─ pyproject.toml          # exposes the `recorder` command; per-OS dependency markers
├─ SPEC.md                 # this file (also the package readme)
├─ README.md               # per-OS setup and troubleshooting
├─ src/recorder/
│  ├─ config.py            # capture constants (samplerate, block size, bitrate)
│  ├─ state.py             # paths, state file, pid liveness, terminate, stop flag, warning note, log tail
│  ├─ mix.py               # pure stereo mix (the unit-tested core)
│  ├─ resample.py          # streaming linear resampler
│  ├─ audio_io.py          # DeviceStream: per-source buffering, stereo conversion, resampling
│  ├─ engine.py            # capture/mix/encode loop over a backend's two sources
│  ├─ _daemon.py           # detached background entry point
│  ├─ cli.py               # argparse subcommands
│  └─ backends/
│     ├─ __init__.py       # select_backend(): lazy import per sys.platform
│     ├─ base.py           # Source / Backend protocols, SourceInfo
│     ├─ polled.py         # PolledSource, shared by the Windows source and the macOS microphone
│     ├─ windows.py        # PyAudioWPatch: WASAPI loopback + default input
│     ├─ macos.py          # catap Core Audio tap + sounddevice microphone
│     └─ linux.py          # parec on the default sink's monitor + the default source
├─ scripts/
│  ├─ diag.py              # records both streams separately and compares them
│  └─ transcribe.py        # faster-whisper transcription (faster-whisper is not a dependency)
├─ tests/                  # unit tests against in-memory fakes; no audio hardware needed
└─ docs/superpowers/       # cross-platform design spec and implementation plan
```

Created at runtime and gitignored: `audio/` (finished mp3s) and `.state/` (`recording.json`,
`stop.flag`, `warning.txt`, `engine.log`).

## Dependencies

Declared in `pyproject.toml` with environment markers, so `pip install -e .` pulls only what
the platform needs:

| OS | Python packages | System requirements |
|----|-----------------|---------------------|
| all | `numpy` (mixing), `lameenc` (mp3) | Python 3.9+ |
| Windows | `PyAudioWPatch` (WASAPI loopback + mic) | none |
| macOS | `sounddevice` (mic); `catap>=0.6.1,<0.7` (system-audio tap, Python 3.11+ only) | macOS 14.2+; System Audio Recording and Microphone permission for the terminal app |
| Linux | none | `parec` and `pactl` from `pulseaudio-utils` (`libpulse` on Arch); PipeWire needs `pipewire-pulse` |

On macOS with Python 3.9 or 3.10 the install succeeds without `catap`, and `recorder start`
fails with a message asking for Python 3.11 or later.

## Known limits

- Mic and call audio are clocked by separate devices, so a very long recording can drift. The
  mixer pairs each microphone block with whatever call audio is buffered at that moment and
  pads a shortfall with silence, so the two streams are paired by availability, not locked
  together; if drift ever bites, split to stereo (mic left / call right) instead of summing.
- Designed for an editable install (`pip install -e`); paths resolve relative to this folder,
  overridable with `RECORDER_HOME`. `RECORDER_MIC_GAIN` (0.1 to 10, default 1.0) scales the
  microphone before mixing.
- According to catap's documentation, macOS delivers zeroed buffers, not an error, when the
  terminal lacks the System Audio Recording permission; this has not been observed here yet.
  The recorder can only notice that the whole call side was silent, and then warns; a
  genuinely silent call produces the same warning. Windows and Linux never warn.
- macOS: the Core Audio tap capture and the "missing permission yields silence" behaviour have
  not been run end to end on real hardware yet; the live smoke test is pending. The backend is
  unit-tested with fakes.
- macOS: `catap` documents that it does not recover from sleep/wake or from an output switch
  that changes the stream format. The recorder ends the recording and saves what it has when
  `catap` reports a failure. None of this has been tested on hardware, including an AirPods
  profile change mid-call.
- macOS: a Mac mini has no built-in input device; a microphone or headset must be connected.
- Linux: the default sink is resolved when recording starts; switching the default output
  mid-recording is not followed. The backend is unit-tested against fakes and was also run end
  to end against a real PulseAudio server in Docker (`scripts/e2e_linux.sh`, null sinks and
  played tones); PipeWire and real audio hardware are unverified.
- Linux: the call lags the microphone by a constant 0.2 to 0.4 s. The loopback's `parec`
  reader holds back its first three blocks (0.3 s) and then streams, which leaves the mixer a
  standing reserve of call audio; without it, a burst that arrived just after its microphone
  block was padded with silence and shifted the rest of the call. Up to about 0.3 s of call
  audio still in the reserve at stop is not mixed. The reserve absorbs about two blocks of
  timing difference between the two streams. An earlier two-block reserve passed 5 of 7 Docker
  end-to-end runs (each failure a single 15 or 20 ms call gap), which is why it is three.
  macOS has no such reserve; whether its call side shows the same gaps is unverified.
- Windows: the backend was moved, not rewritten, and is tested against an in-memory
  PyAudioWPatch fake. It is a behaviour-preserving move apart from reporting a negative
  `available()` as a device error and ignoring blocks delivered after stop.
