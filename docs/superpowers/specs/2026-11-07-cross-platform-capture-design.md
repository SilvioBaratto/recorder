# Cross-platform capture: macOS and Linux support

Date: 2026-11-07
Status: approved, implemented; Linux run end to end against PulseAudio in Docker; macOS and
Windows live verification pending

## Goal

`recorder start | stop | status | devices` produce the same result on Windows, macOS and
Linux: one mp3 containing the call audio (everyone you hear) mixed with your own microphone,
with no echo because nothing is ever played back.

Success criteria:

1. Windows behaviour is unchanged apart from two deliberate differences: a negative
   `available()` from the device is reported as a device error that ends and saves the
   recording, and blocks delivered after stop are ignored. Otherwise the Windows backend is a
   behaviour-preserving move of the earlier code.
2. On this Mac (macOS 27, arm64) a recording made while a video plays and the user speaks
   contains both streams. `recorder devices` explains any problem it can detect.
3. On a stock PulseAudio or PipeWire Linux desktop the same holds, with `parec` and `pactl`
   (package `pulseaudio-utils`) as the only system requirements.
4. `pip install -e .` succeeds on all three platforms, and `pytest` passes on all three
   without any audio hardware or audio libraries installed.

## Non-goals

- A BlackHole or other virtual-driver fallback on macOS (declined during brainstorming).
- Per-application capture, ALSA-only systems, drift correction between mic and call clocks.
- Fixing the Windows-side behaviour in any way.

## What broke before this work

| Where | Problem |
|-------|---------|
| `pyproject.toml` | `PyAudioWPatch` was a hard dependency and is Windows-only, so install failed elsewhere. |
| `audio_io.py` | `import pyaudiowpatch` at module top. `engine.py` imports `audio_io`, so even tests failed to import on macOS and Linux. |
| `audio_io.py` | Device discovery was WASAPI-only (`paWASAPI`, `isLoopbackDevice`, loopback generator). |
| `state.py` | `os.kill(pid, 0)` raising `PermissionError` was read as "dead"; it means the process exists. |
| `state.py` | POSIX `terminate` killed only the daemon pid, not a process group. |
| `cli.py` | The startup check used pid liveness, which reports a not-yet-reaped zombie child as alive. |
| `SPEC.md` | Still described `soundcard`; the code used PyAudioWPatch. |

Already portable and untouched: `mix.py`, `resample.py`, `config.py`, the `lameenc` encoder,
the sentinel-file stop protocol, and the `start_new_session` detach in `cli.py`.

## How each OS exposes the call audio

| OS | Call audio ("loopback") | Microphone |
|----|-------------------------|------------|
| Windows | WASAPI loopback of the default output via PyAudioWPatch | PyAudioWPatch default WASAPI input |
| macOS 14.2+ | Core Audio process tap on the global output mix, through `catap` | `sounddevice` default input (PortAudio is bundled in its macOS wheel) |
| Linux | `parec` on the default sink's `.monitor` source | `parec` on the default source |

macOS has no loopback input device. By design a Core Audio tap needs no driver and no routing
change, so headphones, AirPods and the volume keys should be unaffected (not yet observed on
hardware). It requires macOS 14.2 and Python
3.11 (a `catap` constraint). A Mac mini has no built-in input device, so a microphone or
headset must be connected.

## Architecture

A small interface separates "where audio comes from" from "how it is buffered and mixed".

```
engine.record()
   └─ backend = select_backend()                 # by sys.platform, imports lazily
        ├─ backend.open_loopback() -> Source
        └─ backend.open_microphone() -> Source
   DeviceStream(source, target_rate, stop, log, label)   # start / pull / pull_nowait / join / heard_signal
```

Package `src/recorder/backends/`:

- `base.py`: the `Source` and `Backend` protocols, the `SourceInfo` dataclass (`name`,
  `sample_rate`, `channels`) and the `OnBlock` / `OnEnd` callback types.
  - `Source.start(on_block, on_end)`: begin delivering `float32` blocks of shape
    `(n, channels)` at the source's native rate, by calling `on_block` from a thread the
    source owns. `on_end` is called exactly once when delivery stops, with `None` after a
    normal `close` and with the exception otherwise, so the engine can tell a clean stop from
    a device failure.
  - `Source.close()`: stop and release. Idempotent, and it never blocks on a device read.
  - `Backend.open_loopback()`, `Backend.open_microphone()`: return a `Source`.
    `Backend.describe()` returns the text for `recorder devices`. `Backend.close()` is the
    final teardown. `Backend.silence_hint` is the advice shown when the call side turns out
    silent; empty means the platform has nothing to warn about.
- `polled.py`: `PolledSource`, shared by the Windows source and the macOS microphone (below).
- `windows.py`: `WindowsBackend`, PyAudioWPatch.
- `macos.py`, `linux.py`: described below.
- `__init__.py`: `select_backend()`; raises an actionable `RuntimeError` on unsupported
  platforms. Each backend module is imported inside its branch, so a platform's audio
  library is never imported elsewhere.

`audio_io.py` keeps `DeviceStream`, `_to_stereo` and the resampling and buffering logic. It
imports no audio library; it consumes a `Source` through `on_block` / `on_end`. A source that
fails sets the shared stop event through `DeviceStream`, so the recording is saved up to that
point. `engine.record()` opens the loopback, then the microphone (closing the loopback if the
microphone cannot be opened, and closing both if anything fails before they start), starts
both streams and mixes in a microphone-driven loop: each microphone block sets the timeline
and the loopback contributes whatever is ready, padded with silence (on Linux the loopback
source keeps a reserve, see the Linux backend). The publish-before-teardown invariant holds:
the mp3 is renamed into place, then both sources are closed, then `backend.close()` runs.

### PolledSource

`PolledSource(info, available, read, close_device, start_device, poll_seconds)` adapts a
pull-style device to the push-style `Source`. Its thread polls `available()` and calls
`read(n)` only when frames are waiting; it never calls a blocking read. A silent loopback
never returns from a blocking read, and a thread parked inside a native read crashes the
process when the device is torn down.

- A negative `available()` is a device error code (PortAudio reports an unplugged or
  switched device that way). The stream ends and `on_end` receives an `OSError`, instead of
  recording silence forever.
- `start_device` runs on the caller's thread inside `start`, so a failure to start reaches
  the caller. The macOS microphone uses it to start its `InputStream`.
- `close()` sets the stop event and joins the thread for at most 2 s (it does not join itself
  when called from a callback). The thread releases the device exactly once and then calls
  `on_end`. A source closed before `start` releases the device directly and never calls
  `on_end`.

### Windows backend

`WindowsBackend` holds today's PyAudioWPatch logic: lazy PortAudio start, the default WASAPI
loopback and input lookup, `describe()` in its existing format, and `terminate()` in
`close()`. Each opened stream is wrapped in a `PolledSource` driven by
`get_read_available()`, so the "never block in `read()`" rule moved there from the old
`DeviceStream`. It is a behaviour-preserving move apart from reporting a negative
`available()` as a device error and ignoring blocks delivered after stop. It has been
exercised against an in-memory PyAudioWPatch fake only, never on Windows from this
environment.

### macOS backend

- **Call audio:** a `catap` streaming-only system-audio session (no output file) with an
  `on_buffer` callback. `open_loopback()` starts the tap immediately: `catap` reports the
  native stream format only after the tap starts, and `Source.info` must be known before
  `Source.start()`. Buffers that arrive before `Source.start()` are discarded. The callback
  does the minimum: it converts `AudioBuffer.data` to `float32` according to the buffer's
  format (32-bit float, or 16/32-bit signed integer, little-endian) and calls `on_block`.
  `catap` drops new buffers once more than `max_pending_buffers` (256) are queued for its
  worker, and the capture then fails at stop, so no heavy work happens in the callback. Mute
  stays off, so nothing is silenced. A watcher thread
  polls the session's `capture_failed` flag every 0.1 s and reports a failure through
  `on_end`. `Source.close()` stops the session; when it is called from inside a callback the
  stop moves to a helper thread, because `catap` joins the worker that runs the callback.
- **Microphone:** `sounddevice.InputStream` on the default input at its native rate and at
  most two channels, read through a `PolledSource` with `read_available` / `read`, so no
  read can block at teardown.
- **Preconditions** (checked by `describe()` and by `open_loopback()`): macOS 14.2 or later,
  Python 3.11 or later, `catap` importable. Each failure raises a `RuntimeError` naming the
  fix; `describe()` renders it inline instead.
- **Permissions:** the terminal that launches the recorder needs *System Audio Recording*
  (for the tap) and *Microphone*, and must be restarted after they are granted. According to
  `catap`'s documentation, macOS delivers zeroed buffers instead of an error when the
  system-audio permission is missing, so a missing permission would be visible only as a
  silent call side (not yet observed on hardware); see the silent call-side warning below.
- **Known limits (not verified on hardware):** `catap` documents that it does not recover
  from sleep/wake or from a format-changing output switch, and its source raises "Tap stream
  format changed during capture" when the tap format changes. The backend reports such a
  failure through `on_end`, which ends the recording with what was captured. The watcher only
  sees catap's `capture_failed` flag, and catap's own reason surfaces later from `stop()`, after
  the stream has already ended; so the message logged is the backend's, and it names the
  likely causes (output device or route change, stream format change, a destroyed tap, more
  than 256 queued buffers) and points at `recorder devices`. Whether a real route change, such
  as AirPods moving to the call profile, triggers this has not been tested.

### Linux backend

- **Resolution:** run `pactl info` with `LC_ALL=C` and parse `Default Sink:` and
  `Default Source:`. `LC_ALL=C` is required: a localized system prints translated labels
  (for example `Sink di default:` in Italian) and parsing would fail. The call device is
  `<default sink>.monitor`. This works on PipeWire through `pipewire-pulse`. A missing
  `pactl` or `parec` raises a `RuntimeError` naming the package (`pulseaudio-utils`, or
  `libpulse` on Arch).
- **Microphone:** the default source. A default source whose name ends in `.monitor`, or no
  default source at all, is rejected with a "no microphone" error: recording a monitor as the
  microphone would capture the call twice.
- **Capture:** one `parec` subprocess per device, started with `--format=float32le
  --rate=48000 --channels=2 --latency-msec=100`, so PulseAudio does the format conversion and
  the resampler becomes a no-op. A reader thread does a blocking `read` on the unbuffered pipe
  and carries any partial frame over to the next read. `Source.close()` sends `SIGTERM`,
  then `SIGKILL` after 1 s; the EOF unblocks the reader, which is joined for at most 2 s. A
  `parec` that exits by itself is reported through `on_end` with its exit code and stderr.
  With no PortAudio in the process there is no native teardown crash.
- **Loopback reserve:** with `--latency-msec=100` both streams arrive in bursts of about
  0.1 s, the same size as the 4800-frame mixing block. That is not an advantage: the mixer is
  clocked by the microphone, takes whatever loopback audio is buffered at each microphone
  block and pads the shortfall with silence, and the late frames then shift the rest of the
  call. With no reserve, whichever stream's burst lands first decides an underrun, so a
  loopback burst arriving just after its microphone block, a burst split across two pipe
  reads, or a short stall inserts 25 to 100 ms of silence into the call (seen live, see open
  question 3). A lower latency alone does not remove that race, and making the mixer wait for
  the loopback would stall it on a silent WASAPI loopback. So `ParecSource` takes
  `reserve_frames`: it holds its deliveries back until that many frames have arrived, hands
  them over together, and then delivers every read as it comes, which leaves the downstream
  `DeviceStream` a standing reserve. The loopback uses `2 * BLOCK` (9600 frames, 0.2 s),
  which absorbs up to one extra block of jitter between the two streams; the microphone uses
  none. Frames still held back at `close()` are dropped; if `parec` exits by itself first,
  they are delivered before `on_end` reports the exit. The cost is a constant lag of the call
  behind the microphone, 0.1 to 0.3 s depending on the phase between the two streams, and up
  to about 0.2 s of call audio left unmixed at stop. The engine, `fit_and_mix` and the Windows
  path are unchanged.
- **Orphans:** on a normal stop the daemon closes each `parec` itself (`SIGTERM`, then
  `SIGKILL`) before it exits. Only when the daemon has not exited 15 s after the stop request
  does `recorder stop` terminate the daemon's whole process group, which `parec` belongs to.
  If the daemon dies on its own, `parec` is expected to exit on its next write to the closed
  pipe; that path is not tested.
- **Limit (documented):** the monitor is chosen when recording starts. Switching the default
  output mid-recording is not followed.
- **Status:** unit-tested against in-memory `parec` / `pactl` fakes, and run end to end
  against a real PulseAudio 16.1 server in Docker (`scripts/e2e_linux.sh`, see open question
  3). PipeWire and real audio hardware are unverified.

### Silent call-side warning

`DeviceStream.heard_signal` turns true when any block delivered by the loopback source
carries a non-zero sample. After the mp3 is published, if frames were recorded, the loopback
never carried signal and the backend has a non-empty `silence_hint`, the engine logs a
`WARNING:` line to `engine.log` and writes the hint to `.state/warning.txt`. `recorder stop`
runs in a different process than the daemon, so that file is the channel: `stop` reads and
deletes it and prints `Warning: ...` after the saved path, only when an mp3 was produced.
`stop` reads the file only after publishing the mp3, and a file it cannot read (a directory,
no permission, bytes that are not UTF-8) counts as no warning, so the note never costs the
recording. `recorder start` clears a stale warning. A failure to write the file does not fail
the recording.

Only the macOS backend sets a hint. It is a warning, not an error: a genuinely silent call
must still save the mic.

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

Linux has no Python audio dependency; it needs `parec` and `pactl` on `PATH`.
`requires-python` stays `>=3.9` so Windows is unaffected. On macOS with Python 3.9 or 3.10 the
install succeeds without `catap` and `recorder start` fails with "capturing call audio on
macOS needs Python 3.11 or later ...". `catap` is pinned below 0.7 because the 0.6 line is what
the backend was written against, and its README states that sleep/wake and format-changing
output switches are not recovered.

## Process-control hardening (POSIX)

- `pid_alive`: first reaps an exited child with `os.waitpid(pid, os.WNOHANG)`, because a
  zombie child still answers signal 0 and would look alive; a pid that is not our child
  raises `ChildProcessError`, which falls through. Then `os.kill(pid, 0)`:
  `ProcessLookupError` means dead; `PermissionError` means alive.
- `terminate`: `SIGTERM` first, a 2 s grace period, then `SIGKILL` if the process survives.
  The signal goes to the process group only when the pid leads its own group
  (`os.getpgid(pid) == pid`); otherwise it goes to the pid alone. `os.killpg(pid, ...)` on a
  pid that leads no group does not reach another group: it fails with `ESRCH`, since no group
  has that id, and the process would go unsignalled. The hazard the check also rules out is
  `os.killpg(os.getpgid(pid), ...)` on a non-leader, which would signal the group the pid
  belongs to, possibly the caller's own. The daemon leads its group through
  `start_new_session`.
- `cmd_start`: the startup check uses `proc.poll()` on the child it just spawned instead of
  pid liveness. This applies on every OS and is equivalent on Windows, where an exited child is
  reported by `poll()` the same way.
- The `os.name == "nt"` branches of `pid_alive` and `terminate` are untouched.

## Error handling

- Every environment problem (wrong OS version, missing package, missing permission signal,
  missing `parec`, no default sink or source) is a `RuntimeError` with an actionable message.
  The daemon logs it and the existing `cmd_start` path prints the log tail.
- A source that errors at runtime reports it through `on_end`; `DeviceStream` logs it and
  sets `stop_event`, so the recording is saved up to that point.

## Testing

1. **Existing tests** keep their intent. The daemon tests drive the real engine and
   `WindowsBackend` through `tests/fake_pyaudio.py`, which keeps the two modelled WASAPI
   behaviours: a silent loopback that never delivers, and a teardown that crashes if a reader
   is still inside the device. `tests/fake_backend.py` is a platform-free `Backend` /
   `Source` pair (live or silent sources) for the engine-level tests. Imports stay free of
   audio libraries so the suite passes on all three platforms.
2. **Backend and process-control tests:**
   - `select_backend()` per `sys.platform`, and subprocess checks that the core modules and
     the Linux and macOS backend modules import no audio library.
   - `PolledSource` lifecycle: polling without blocking reads, close from the source thread,
     negative `available()`, failed thread start.
   - Windows backend against the in-memory PyAudioWPatch fake.
   - macOS precondition checks, sample-format conversion (`float32`, `int16`, `int32`, mono
     and stereo), the tap lifecycle against a fake `catap`, and the microphone.
   - Linux `pactl info` parsing (including the real `(null)` output) and the `LC_ALL=C`
     environment, `parec` frame reassembly across split reads, and `SIGTERM` / `SIGKILL`
     shutdown against a fake subprocess.
   - The Linux loopback reserve: scripted `parec` arrivals (late bursts, split reads, a
     250 ms stall flushed as 64 KiB reads, a two-block microphone backlog) replayed through
     `ParecSource`, `DeviceStream` and `fit_and_mix`, with no silence inside the call with the
     reserve and the expected gaps without it.
   - The silent-call-side warning through the daemon and through `recorder stop`.
   - The `pid_alive` and `terminate` changes against real child processes, and the
     `cmd_start` failure check against a child that exits immediately.
   - Per-platform dependency markers in `pyproject.toml`.
3. **Live macOS smoke test (this machine), pending:** after the user grants the terminal
   *System Audio Recording* and *Microphone*, run `recorder start`, play audio and speak for
   about 10 s, run `recorder stop`, and confirm both streams are audible (`scripts/diag.py`
   prints per-stream RMS). The same run answers the open questions below.
4. **Linux:** `scripts/e2e_linux.sh` runs the unit tests in an `ubuntu:24.04` Docker
   container (success criterion 4 on Linux), then the backend end to end against PulseAudio,
   with null sinks and played tones. It fails on any test failure, on a missing default device
   that is not reported as such, on a missing tone, and on any dropout in the call tone.
   Results are under open questions 3 and 4.
5. **Windows:** cannot be run here. The backend is a behaviour-preserving move, so the check
   is a review of the diff plus a manual run by the user.

## Documentation

`SPEC.md` (also the package readme) describes the three backends, the layout and the
dependencies per OS. `README.md` carries the per-OS setup: permissions on macOS,
`pulseaudio-utils` on Linux, Python 3.11+ for macOS, and troubleshooting. `scripts/diag.py`
goes through the backend interface.

## Open questions to settle by experiment, not by reading

1. Does macOS attribute the *System Audio Recording* permission to the terminal when the daemon
   is spawned detached with `start_new_session`? Untested. If not, the fix goes in `cmd_start`,
   and this spec gets amended before that work. Until then, the silent call-side warning is
   the only symptom a user sees.
2. Does `catap`'s global tap survive an output-device format change, such as AirPods switching
   to the call profile mid-meeting? `catap` documents that it does not recover from
   format-changing transitions or from sleep/wake; whether and how that surfaces in a real
   recording is untested. The backend ends the recording and saves what was captured when
   `catap` reports a failure. If a route change instead leaves the tap delivering silence or
   nothing, there is no stop-time warning once the call was audible earlier: the warning fires
   only when the loopback never carried a non-zero sample in the whole recording, and
   `heard_signal` stays true after the first audible block. The only traces are the
   `loopback capture error` line in `engine.log` (written when `catap` reports a failure, see
   the watcher in `macos.py`) and a recording that ends early; a tap that silently stops
   delivering signal leaves no trace at all. The limit is documented, not mitigated.
3. Does `parec -d <sink>.monitor` behave the same on PipeWire as on PulseAudio, including the
   0.1 s burst size that `--latency-msec=100` is meant to give? **PulseAudio: answered.** On
   2026-11-09 `scripts/e2e_linux.sh` ran the backend against PulseAudio 16.1 in an
   `ubuntu:24.04` container (aarch64, Docker on colima), with a null sink `virt` as the default
   sink and a `module-virtual-source` named `fakemic` (fed from a second null sink) as the
   default source. An 880 Hz tone played into `virt` and a 440 Hz tone played into the
   microphone's null sink both came out of one `recorder start` / `recorder stop` mp3, in four
   runs, each FFT peak at least 1.7 x 10^4 times the median spectral magnitude (the script
   requires 20 times, and 10% of the other tone); `stop` printed `Saved` and no `Warning:`.
   With `virt.monitor` as the default source, `recorder start` failed with "no microphone: the
   sound server has no default input device". `parec` with the backend's arguments delivered
   4800-frame (0.1 s) reads every 100 ms, at most 110 ms apart, from both the monitor and the
   virtual source. Seen in those first runs: in 3 of the 6 recordings checked for gaps, the
   call side started 0.1 s behind the microphone and had one to three gaps of 20 to 60 ms,
   while a raw `parec` capture of each side alone had none.
   **Explained and mitigated, not eliminated.** The gaps were silence the microphone-clocked
   mixer inserted when a loopback burst was not buffered yet (see the loopback reserve under
   the Linux backend); the 25 and 60 ms gaps match a 4800-frame burst read as 3600 + 1200 and
   1920 + 2880 frames around a microphone block. With the two-block reserve, the script (which
   now also runs the unit tests in the container and fails on any call-side dropout) passed in
   5 of 7 runs on 2026-11-09; each of the other two had a single gap of 15 or 20 ms in the call
   tone. A traced run of the same rig, with the engine in-process and an independent raw
   `parec` capture of `virt.monitor` alongside, showed that the remaining gaps are still mixer
   pads: the raw capture was gap-free in all 30 recordings, while the recordings had an audible
   call gap in 4 of 10 with no reserve, 1 of 10 with two blocks and 0 of 10 with three blocks.
   Under that load both streams' reads were irregular (about 100 to 7400 frames), and the
   two-block gap came when the microphone's deliveries ran more than a block ahead of the
   loopback's. A three-block reserve (0.1 s more lag) or placing the loopback on the
   microphone's timeline in the engine would close the remaining gaps; neither is implemented.
   With `virt.monitor` as the default source, `recorder start` now fails with "no microphone:
   the default input is 'virt.monitor', a monitor of an output; ...".
   **Still open:** PipeWire through `pipewire-pulse`, and hardware sinks and microphones;
   nothing has run against either.
4. What does `pactl info` print when there is no default sink or source? **Answered** for
   PulseAudio 16.1 with no sink and no source loaded: the lines are present and read
   `Default Sink: (null)` and `Default Source: (null)`, not `n/a`, an empty value or
   `@DEFAULT_SINK@`. At first `parse_pactl_info` took `(null)` for a device name, so
   `recorder devices` listed `(null).monitor` and `(null)`, and `recorder start` failed with
   `parec` reporting "Stream error: Invalid argument" followed by `NoAudioCaptured`. It now
   treats `(null)`, `n/a`, an empty value and any `@...` placeholder as missing: `recorder
   devices` shows both lines as `UNAVAILABLE (no default output device; ...)` and
   `UNAVAILABLE (no microphone: ...)`, and `recorder start` fails with "no default output
   device; select one with 'pactl set-default-sink <name>' ...". `scripts/e2e_linux.sh` asserts
   this against the real server, and it held in all 7 runs.

Settled: the Python 3.9 floor. On 2026-11-09, with macOS's `/usr/bin/python3` (3.9.6), the
install succeeded without `catap`, the full suite passed (149 tests), and `recorder start` exited
1 with the "needs Python 3.11 or later" message in its log tail and left no daemon running.
