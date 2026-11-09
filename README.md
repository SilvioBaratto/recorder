# recorder

Records a meeting's audio, both the other participants and your own voice, to a single mp3
with no echo: the call is captured from the machine's output and the microphone is mixed in,
and nothing is ever played back. It runs on Windows, macOS and Linux. `SPEC.md` has the
design and the layout.

```
recorder start     # begin capturing in the background, returns immediately
recorder stop      # finish and save audio/recording-YYYYMMDD-HHMMSS.mp3
recorder status    # is a recording in progress?
recorder devices   # show which devices would be captured, and what is wrong with them
```

The mp3 lands in `audio/` inside this folder, and `.state/engine.log` is the log of the
background process. Set `RECORDER_HOME` to use another folder for both. If the path to this
folder contains spaces, quote it, for example
`"/path with spaces/recorder/.venv/bin/recorder" start`.

Commands below run from this folder.

## macOS

Needs macOS 14.2 or later, Python 3.11 or later, and a microphone.

Install:

```
uv venv --python 3.12 && uv pip install -e .
.venv/bin/recorder devices
```

`pip install -e .` works too, inside a virtualenv built on Python 3.11 or later. The call
audio comes from a Core Audio process tap (through `catap`). By design a tap needs no virtual
audio driver and no routing change, so your output device, headphones and volume keys should
not be affected; that has not been observed on this project's hardware yet.

Permissions: the terminal app that launches `recorder` needs two permissions in System
Settings > Privacy & Security:

- Screen & System Audio Recording, enabled in the "System Audio Recording Only" list.
- Microphone.

Quit the terminal app completely and reopen it after granting them; macOS applies the
permission only to a restarted app. The exact wording of these settings can differ between
macOS releases.

Verify with `.venv/bin/recorder devices`. A healthy result looks like this:

```
Call loopback source : System audio (Core Audio tap)
Microphone (your voice): <your input device> @ <rate> Hz

Grant your terminal app System Audio Recording and Microphone access in System Settings > Privacy & Security, then restart it.
```

The first line only proves that macOS, Python and `catap` are usable; the recorder cannot see
whether the permission was granted. The last line is always printed. So also make a short
test: run `recorder start`, play some audio and speak for about 10 seconds, run
`recorder stop`, and listen to the mp3. If `stop` prints a `Warning:` line, see
Troubleshooting.

Status: the macOS backend is unit-tested with fakes. The Core Audio tap capture and the
"missing permission yields silence" behaviour have not been run end to end on real hardware
yet; the live smoke test is pending.

## Linux

Needs PulseAudio, or PipeWire with `pipewire-pulse`, Python 3.9 or later, and the `parec`
and `pactl` tools. They come from the `pulseaudio-utils` package (`libpulse` on Arch Linux).
On Debian or Ubuntu, `python3-venv` provides the `venv` module used below:

```
sudo apt install pulseaudio-utils python3-venv
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/recorder devices
```

The call is recorded from the monitor of the default output (`<default sink>.monitor`) and
your voice from the default input. No permissions are involved.

A healthy `recorder devices` looks like this:

```
Call loopback source : alsa_output.<device>.analog-stereo.monitor @ 48000 Hz
Microphone (your voice): alsa_input.<device>.analog-stereo @ 48000 Hz

All loopback endpoints:
  - alsa_output.<device>.analog-stereo.monitor
```

Status: the Linux backend is unit-tested with fakes. It has not yet been run against a real
PulseAudio or PipeWire server.

## Windows

Needs Python 3.9 or later. Installing pulls in `PyAudioWPatch`; no permissions or extra
software are needed:

```
python -m venv .venv
.venv\Scripts\activate
pip install -e .
recorder devices
```

The call is the WASAPI loopback of the default speaker (on a devbox that is the "Remote
Audio" device) and your voice is the default input. A healthy `recorder devices` looks like
this:

```
Call loopback source : <speaker> [Loopback] @ 48000 Hz
Microphone (your voice): <microphone> @ 48000 Hz

All loopback endpoints:
  - <speaker> [Loopback]
```

Windows behaviour is unchanged. The backend was moved into `backends/windows.py` without
logic changes and is covered by tests against an in-memory PyAudioWPatch fake; it has not
been run on Windows since the move.

## Troubleshooting

**macOS: the call is silent in the mp3, or `recorder stop` prints a `Warning:` line.**
According to catap's documentation, a process without the System Audio Recording permission
gets zeroed audio buffers from macOS instead of an error; this has not been observed here yet.
When the whole call side of a recording was silent, the recorder logs a `WARNING:` line in
`.state/engine.log` and `recorder stop` prints it. Grant the permission to the terminal
app you run `recorder` from (see the macOS section), quit that app completely, reopen it and
record again. The same warning appears when the call genuinely was silent. Whether macOS
attributes the permission to the terminal when `recorder start` runs the daemon in the
background has not been tested; if the warning persists after granting and restarting, report
it.

**macOS: `recorder devices` says the microphone is unavailable.**
A Mac mini has no built-in input device, so connect a microphone or headset and select it in
System Settings > Sound > Input. Without an input the output looks like this:

```
Microphone (your voice): UNAVAILABLE (no microphone available (Error querying device -1): choose an input device in System Settings > Sound > Input, and grant your terminal app Microphone access in System Settings > Privacy & Security > Microphone)
```

**macOS: `recorder start` fails with "needs Python 3.11 or later".**
`catap` only installs on Python 3.11 or later, so on Python 3.9 or 3.10 the install succeeds
without it. Delete `.venv` and install again with `uv venv --python 3.12 && uv pip install -e .`.

**Linux: "pactl and parec not found" (or just one of them).**
Install `pulseaudio-utils` (`libpulse` on Arch Linux). The message appears in
`recorder devices`, and in the output of `recorder start` when the daemon fails.

**Linux: "no microphone: the sound server has no default input device".**
There is no default input, or the default input is a monitor of an output; recording it would
capture the call twice, so it is refused. List the sources with `pactl list short sources` and
choose a microphone with `pactl set-default-source <name>`.

**`recorder start` prints "Recording failed to start".**
The last lines of `.state/engine.log` are printed with it; they name the missing package,
permission or device.

## Known limits

- macOS: `catap` does not recover from sleep/wake or from an output switch that changes the
  stream format ("Tap stream format changed during capture"). The recorder ends the recording
  and saves what it has when `catap` reports such a failure. This has not been tested on
  hardware, including an AirPods profile change mid-call.
- Linux: the default output is resolved when recording starts; switching it mid-recording is
  not followed.
- Only macOS warns about a silent call side. Windows and Linux never do.
- The microphone and the call are clocked by separate devices, so a very long recording can
  drift between them.

## Development

```
pip install -e ".[dev]"
pytest
```

The tests use in-memory fakes and need no audio hardware or audio libraries. `scripts/diag.py`
records the call and the microphone separately and prints their levels, which helps when one
of them comes out silent.
