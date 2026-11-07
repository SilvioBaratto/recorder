# Cross-platform capture: macOS and Linux support

Date: 2026-11-07
Status: draft, awaiting review

## Goal

`recorder start | stop | status | devices` produce the same result on Windows, macOS and
Linux: one mp3 containing the call audio (everyone you hear) mixed with your own microphone,
with no echo because nothing is ever played back.

Success criteria:

1. Windows behaviour is unchanged. The Windows backend is a pure move of today's code.
2. On this Mac (macOS 27, arm64) a recording made while a video plays and the user speaks
   contains both streams. `recorder devices` explains any problem it can detect.
3. On a stock PulseAudio or PipeWire Linux desktop the same holds, with `parec` as the only
   system requirement.
4. `pip install -e .` succeeds on all three platforms, and `pytest` passes on all three
   without any audio hardware or audio libraries installed.

## Non-goals

- A BlackHole or other virtual-driver fallback on macOS (declined during brainstorming).
- Per-application capture, ALSA-only systems, drift correction between mic and call clocks.
- Fixing the Windows-side behaviour in any way.

## What breaks today

| Where | Problem |
|-------|---------|
| `pyproject.toml:12` | `PyAudioWPatch` is a hard dependency and is Windows-only, so install fails elsewhere. |
| `audio_io.py:15` | `import pyaudiowpatch` at module top. `engine.py:13` imports `audio_io`, so even tests fail to import on macOS and Linux. |
| `audio_io.py:29-64` | Device discovery is WASAPI-only (`paWASAPI`, `isLoopbackDevice`, loopback generator). |
| `state.py:156-159` | `os.kill(pid, 0)` raising `PermissionError` is read as "dead"; it means the process exists. |
| `state.py:170` | POSIX `terminate` kills only the daemon pid, not a process group. |
| `cli.py:58` | Startup check uses pid liveness, which reports a not-yet-reaped zombie child as alive. |
| `SPEC.md` | Still describes `soundcard`; the code uses PyAudioWPatch. |

Already portable and untouched: `mix.py`, `resample.py`, `config.py`, the `lameenc` encoder,
the sentinel-file stop protocol, and the `start_new_session` detach in `cli.py`.

## How each OS exposes the call audio

| OS | Call audio ("loopback") | Microphone |
|----|-------------------------|------------|
| Windows | WASAPI loopback of the default output via PyAudioWPatch (today's code) | PyAudioWPatch default WASAPI input |
| macOS 14.2+ | Core Audio process tap on the global output mix, through `catap` | `sounddevice` default input (PortAudio is bundled in its macOS wheel) |
| Linux | `parec` on the default sink's `.monitor` source | `parec` on the default source |

macOS has no loopback input device. A Core Audio tap needs no driver and no routing change, so
headphones, AirPods and the volume keys behave normally. It requires macOS 14.2 and Python
3.11 (a `catap` constraint).

## Architecture

A small interface separates "where audio comes from" from "how it is buffered and mixed".

```
engine.record()
   └─ backend = select_backend()                 # by sys.platform, imports lazily
        ├─ backend.open_loopback() -> Source
        └─ backend.open_microphone() -> Source
   DeviceStream(source, target_rate, stop, log, label)   # unchanged API: start/pull/pull_nowait/join
```

New package `src/recorder/backends/`:

- `base.py`: the `Source` and `Backend` protocols and a `SourceInfo` dataclass
  (`name`, `sample_rate`, `channels`).
  - `Source.start(on_block)`: begin delivering `float32` blocks of shape `(n, channels)` at the
    source's native rate by calling `on_block` from a thread the source owns.
  - `Source.close()`: stop and release. Must be safe to call twice and must never block on a
    device read.
  - `Backend.open_loopback()`, `Backend.open_microphone()`, `Backend.describe()` (text for
    `recorder devices`) and `Backend.close()` (final teardown).
- `windows.py`: today's PyAudioWPatch code, moved. It owns the polling reader thread built on
  `get_read_available()`; the "never block in `read()`" rule moves with it.
- `macos.py`, `linux.py`: described below.
- `__init__.py`: `select_backend()`; raises an actionable `RuntimeError` on unsupported
  platforms.

`audio_io.py` keeps `DeviceStream`, `_to_stereo` and the resampling and buffering logic. It no
longer imports any audio library; it consumes a `Source` through `on_block`. `engine.record()`
and `_daemon.main()` change only where devices come from. The publish-before-teardown
invariant is preserved: the mp3 is renamed into place, then sources are closed, then
`backend.close()`.

### Windows backend

Moves `open_pyaudio`, `find_loopback_device`, `find_input_device`, `describe_devices` and the
PortAudio-opening half of `DeviceStream.__init__/_run` into `windows.py` with no logic change.
The polling thread there calls `on_block` instead of resampling directly.

### macOS backend

- **Call audio:** a `catap` streaming-only system-audio session (no output file) with an
  `on_buffer` callback. The callback does the minimum, converting `AudioBuffer.data` to
  `float32` according to `AudioBuffer.format` (`float` 32-bit, or `signed_integer` 16/32-bit)
  and calling `on_block`. `catap` fails the capture if more than `max_pending_buffers` (256)
  are queued, so no heavy work happens in the callback. Mute stays off, so nothing is
  silenced. `Source.close()` calls `session.stop()` from the owning thread, never from inside
  `on_buffer`.
- **Microphone:** `sounddevice.InputStream` on the default input at its native rate and
  channel count, polled with `read_available`/`read` so no read can block at teardown.
- **Preconditions** (checked by `describe()` and by `open_loopback()`): macOS 14.2 or later,
  Python 3.11 or later, `catap` importable. Each failure raises a `RuntimeError` naming the fix.
- **Permissions:** the terminal that launches the recorder needs *System Audio Recording* (for
  the tap) and *Microphone*. macOS delivers zeroed buffers instead of an error when the
  system-audio permission is missing, so the engine tracks whether the loopback ever produced a
  non-zero sample. If the whole recording was silent on the call side, `engine.log` and
  `recorder stop` print a warning naming the permission. It is a warning, not an error: a
  genuinely silent call must still save the mic.

### Linux backend

- **Resolution:** run `pactl info` and parse `Default Sink:` and `Default Source:`. The call
  device is `<default sink>.monitor`. This works on PipeWire through `pipewire-pulse`. A
  missing `pactl` or `parec` raises a `RuntimeError` naming the package (`pulseaudio-utils`,
  or `libpulse` on Arch).
- **Capture:** one `parec` subprocess per device, asked for `--format=float32le --rate=48000
  --channels=2`, so PulseAudio does the format conversion and the resampler becomes a no-op.
  A reader thread does a blocking `read` on the pipe and carries any partial frame over to the
  next read. `Source.close()` sends `SIGTERM`, then `SIGKILL` after a short wait; the EOF
  unblocks the reader. With no PortAudio in the process there is no native teardown crash.
- **Orphans:** if the daemon is killed, `parec` gets `SIGPIPE` on its next write and exits.
- **Limit (documented):** the monitor is chosen when recording starts. Switching the default
  output mid-recording is not followed.

## Dependencies

```toml
dependencies = [
    "numpy>=1.23",
    "lameenc>=1.7",
    "PyAudioWPatch>=0.2.12.7; sys_platform == 'win32'",
    "sounddevice>=0.4.6; sys_platform == 'darwin'",
    "catap>=0.6.1,<0.7; sys_platform == 'darwin' and python_version >= '3.11'",
]
```

`requires-python` stays `>=3.9` so Windows is unaffected. On macOS with Python 3.9 or 3.10 the
install succeeds without `catap` and `recorder start` fails with "macOS capture needs Python
3.11+ (try `uv tool install` / `uv venv --python 3.11`)". `catap` is pinned below 0.7 because
the release is two days old and its README states that sleep/wake and format-changing output
switches are not recovered.

## Process-control hardening (POSIX)

- `pid_alive`: `ProcessLookupError` means dead; `PermissionError` means alive.
- `terminate`: on POSIX use `os.killpg` (the daemon is its own session leader through
  `start_new_session`), `SIGTERM` first, `SIGKILL` if it survives.
- `cmd_start`: the startup check uses `proc.poll()` on the child it just spawned instead of
  pid liveness. This applies on every OS and is equivalent on Windows, where an exited child is
  reported by `poll()` the same way.
- The `os.name == "nt"` branches of `pid_alive` and `terminate` are untouched.

## Error handling

- Every environment problem (wrong OS version, missing package, missing permission signal,
  missing `parec`) is a `RuntimeError` with an actionable message. The daemon logs it and the
  existing `cmd_start` path prints the log tail.
- A source that errors at runtime sets `stop_event`, as `DeviceStream` does today, so the
  recording is saved up to that point.

## Testing

1. **Existing tests** keep their intent. `fake_pyaudio.py` becomes a fake `Backend`/`Source`
   pair that keeps both modelled behaviours: a silent loopback that never delivers, and a
   teardown that crashes if a reader is still inside the device. Imports stay free of audio
   libraries so the suite passes on all three platforms.
2. **New unit tests:**
   - `select_backend()` per `sys.platform`, with lazy imports.
   - macOS sample-format conversion (`float32`, `int16`, `int32`, mono and stereo).
   - Linux `pactl info` parsing, `parec` frame reassembly across split reads, and
     `SIGTERM`/`SIGKILL` shutdown against a fake subprocess.
   - The silent-call-side warning, and the `pid_alive`/`terminate`/`cmd_start` changes.
3. **Live macOS smoke test (this machine):** after the user grants the terminal *System Audio
   Recording* and *Microphone*, run `recorder start`, play audio and speak for about 10 s,
   run `recorder stop`, and confirm both streams are audible (ported `scripts/diag.py` prints
   per-stream RMS). The same run answers the open questions below.
4. **Linux:** an automated end-to-end run in a Docker container with a PulseAudio null sink
   and a played tone, if Docker is available. If it is not, Linux is verified by the unit
   tests only and the README says so.
5. **Windows:** cannot be run here. The backend is a pure move, so the check is a review of
   the diff plus a manual run by the user.

## Documentation

Rewrite `SPEC.md` (also the package readme) for the three backends, and fill `README.md` with
per-OS setup: permissions on macOS, `pulseaudio-utils` on Linux, Python 3.11+ for macOS. Port
`scripts/diag.py` to the backend interface.

## Open questions to settle by experiment, not by reading

1. Does macOS attribute the *System Audio Recording* permission to the terminal when the daemon
   is spawned detached with `start_new_session`? If not, the fix goes in `cmd_start`, and this
   spec gets amended before that work.
2. Does `catap`'s global tap survive an output-device format change, such as AirPods switching
   to the call profile mid-meeting? If not, document the limit and warn at stop.
3. Does `parec -d <sink>.monitor` behave the same on PipeWire as on PulseAudio?
   (Docker covers PulseAudio only.)
