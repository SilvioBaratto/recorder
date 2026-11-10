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
recorder transcribe  # turn the newest mp3 into a text file with OpenAI (see Transcribe)
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
your voice from the default input. No permissions beyond the OS defaults are involved.

A healthy `recorder devices` looks like this:

```
Call loopback source : alsa_output.<device>.analog-stereo.monitor @ 48000 Hz
Microphone (your voice): alsa_input.<device>.analog-stereo @ 48000 Hz

All loopback endpoints:
  - alsa_output.<device>.analog-stereo.monitor
```

Status: the Linux backend is unit-tested with fakes and was also run end to end against a real
PulseAudio 16.1 server in Docker (`bash scripts/e2e_linux.sh`: Ubuntu 24.04, null sinks,
played tones). That script also runs the unit tests in the container and fails on any gap in
the call side. With the three-block call-side reserve described under Known limits, 10 of 10
runs passed with no gap in the call (an earlier two-block reserve passed 5 of 7, each failure a
single 15 or 20 ms gap). It has not been run against PipeWire or real audio hardware.

## Windows

Needs Python 3.9 or later. Installing pulls in `PyAudioWPatch`; no extra software and no
permissions beyond the OS defaults are needed:

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

The backend was moved into `backends/windows.py` as a behaviour-preserving move, apart from
two changes: a negative frame count from the device (`available()`) is reported as a device
error that ends the recording and saves it, and blocks delivered after stop are ignored. It is
covered by tests against an in-memory PyAudioWPatch fake; it has not been run on Windows
since the move.

## Transcribe

`recorder transcribe` turns a recording into a plain-text transcript with OpenAI's
`gpt-transcribe` model. It is a batch job, not live captioning, and it handles recordings of
any length, including 2 hours and more: the mp3 is decoded and cut into chunks of about 20
minutes, each cut placed at the quietest moment near the mark so no word is split, every chunk
is sent as a small mono mp3, and the texts are joined.

Install the optional libraries and export your key (the key is read from the environment only):

```
pip install -e ".[transcribe]"
export OPENAI_API_KEY=sk-...
```

```
recorder transcribe                                   # newest mp3 in audio/ -> audio/<name>.txt
recorder transcribe audio/recording-20260101-100000.mp3   # a specific recording
recorder transcribe --languages it,en                 # hint the spoken languages (default: auto-detect)
recorder transcribe --keywords "AC-42, Premium Plus"  # names and jargon to get right
recorder transcribe --force                           # overwrite an existing .txt
```

- The audio is uploaded to OpenAI. OpenAI lists the model at $0.0045 per audio minute, about
  $0.54 for 2 hours; check their pricing page for the current figure.
- The output is text only: no timestamps and no speaker labels, because the recording is one
  mixed track and `gpt-transcribe` does not label speakers.
- If a chunk still fails after the client's retries, or you press Ctrl-C, the finished chunks
  are saved to `<name>.txt.partial` and the command names the chunk that failed. Running it
  again starts over.
- A recording with no speech at all writes nothing; that usually means the capture devices were
  wrong, so run `recorder devices`.

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

**Linux: "no microphone: ...".**
There is no default input ("the sound server has no default input device"), or the default
input is a monitor of an output ("the default input is '<name>.monitor', a monitor of an
output"); recording a monitor would capture the call twice, so it is refused. List the sources
with `pactl list short sources` and choose a real microphone with
`pactl set-default-source <name>`, or select one in the sound settings.

**Linux: "no default output device; select one with 'pactl set-default-sink <name>'".**
The sound server has no default output, so there is no monitor to record the call from;
`pactl info` then shows `Default Sink: (null)`. List the outputs with `pactl list short sinks`
and choose one with `pactl set-default-sink <name>`.

**macOS: the recording ended early, and `.state/engine.log` has a `loopback capture error` line
saying "Core Audio reported a system-audio capture failure while recording".**
The Core Audio tap stopped delivering audio. The message lists the likely causes: the output
device or route changed, the stream format changed, the tap was destroyed, or more than 256
audio buffers queued up unprocessed. The audio captured up to that point is saved. Check the
devices with `recorder devices` and start a new recording. This has not been observed on
hardware yet.

**`recorder start` prints "Recording failed to start".**
The last lines of `.state/engine.log` are printed with it; they name the missing package,
permission or device.

## Known limits

- macOS: `catap` does not recover from sleep/wake or from an output switch that changes the
  stream format. The recorder ends the recording and saves what it has when `catap` reports
  such a failure (see Troubleshooting). This has not been tested on hardware, including an
  AirPods profile change mid-call.
- Linux: the default output is resolved when recording starts; switching it mid-recording is
  not followed.
- Linux: the call lags your voice by a constant 0.2 to 0.5 s, depending on how the two
  streams' first bursts line up (0.4 s in 9 of 10 Docker test runs, 0.5 s in one). The
  recorder holds back the first 0.3 s of call audio as a reserve, so that `parec` bursts
  arriving a moment late do not leave gaps in the call. Because of the lag, the last stretch of
  call audio before you stop, as long as the lag, is not in the file. The reserve absorbs about
  0.2 s of timing difference between the two streams; a longer stall could still leave a short
  gap in the call. macOS has no such reserve, and whether its call side shows the same short
  gaps is unverified.
- Only macOS warns about a silent call side. Windows and Linux never do.
- The microphone and the call are clocked by separate devices, so a very long recording can
  drift between them.

## Development

```
pip install -e ".[dev,transcribe]"
pytest
```

The tests use in-memory fakes and need no audio hardware, audio libraries or API key. Without
the `transcribe` extra, the tests that decode real mp3s are skipped. `scripts/diag.py`
records the call and the microphone separately and prints their levels, which helps when one
of them comes out silent.
