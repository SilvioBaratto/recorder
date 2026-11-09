#!/usr/bin/env bash
# End-to-end check of the Linux backend against a real PulseAudio server, inside a disposable
# ubuntu:24.04 container. Run from the repository root on a host with Docker:
#
#   bash scripts/e2e_linux.sh
#
# The repository is mounted read-only and copied inside the container, so nothing is written
# into the host checkout. The container needs network access for apt and pip. PipeWire is not
# covered.
#
# It fails on: a unit-test failure on Linux, a missing default device that is not reported as
# such, a tone missing from the recording, any dropout on the call side, and an echo guard that
# accepts a monitor as the microphone.
set -euo pipefail

IMAGE="ubuntu:24.04"
SRC=/src
TESTER=tester
WORK="/home/$TESTER/recorder"
CONTAINER_NAME="recorder-e2e-$$"

TONE_CHECK=$(cat <<'PY'
import subprocess
import sys

import numpy as np

RATE = 48000
MIN_RATIO = 20.0
MIN_SHARE = 0.1
TONES = (("voice 440 Hz", 440.0), ("call  880 Hz", 880.0))

raw = subprocess.run(
    ["ffmpeg", "-v", "error", "-i", sys.argv[1], "-f", "f32le", "-ac", "2", "-ar", str(RATE), "-"],
    check=True, capture_output=True,
).stdout
mono = np.frombuffer(raw, dtype="<f4").reshape(-1, 2).mean(axis=1)
print(f"decoded {len(mono) / RATE:.2f} s of audio")

hop = RATE // 10
rms = np.array([np.sqrt(np.mean(mono[i:i + hop] ** 2)) for i in range(0, len(mono) - hop + 1, hop)])
if rms.size == 0 or rms.max() == 0.0:
    sys.exit("FAIL: the recording is silent")
# The tones start and stop at times set by process scheduling, so the analysed window is the
# middle half of the loud stretch rather than fixed offsets into the file.
active = np.flatnonzero(rms > 0.25 * rms.max())
first, last = int(active[0]), int(active[-1])
span = last - first + 1
segment = mono[(first + span // 4) * hop:(first + 3 * span // 4) * hop]
print(f"tones audible from {first / 10:.1f} s to {(last + 1) / 10:.1f} s; "
      f"analysing {len(segment) / RATE:.2f} s from the middle")

spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
freqs = np.fft.rfftfreq(len(segment), 1.0 / RATE)
# Above about 16 kHz the mp3 encoder's low-pass leaves near-zero bins that would flatter the floor.
floor = float(np.median(spectrum[(freqs > 50) & (freqs < 15000)]))
magnitudes = {hz: float(spectrum[np.abs(freqs - hz) <= 5.0].max()) for _, hz in TONES}
strongest = max(magnitudes.values())
present = {}
for label, hz in TONES:
    magnitude = magnitudes[hz]
    ratio = magnitude / floor if floor else float("inf")
    share = magnitude / strongest
    print(f"{label}: magnitude {magnitude:.1f}, {ratio:.0f}x the median spectral magnitude "
          f"{floor:.4f}, {share:.0%} of the stronger tone")
    # A clean synthetic mix has a median magnitude near zero, so an absent tone's leakage still
    # clears 20x the floor; it must also be a sizeable share of the other tone, which plays at the
    # same level.
    present[hz] = ratio >= MIN_RATIO and share >= MIN_SHARE

# Where each tone sits in the file and where it drops out. A gap inside a steady tone means the
# mixer padded that side with silence; on the call side that fails the run.
envelope_frames = RATE // 200
all_freqs = np.fft.rfftfreq(len(mono), 1.0 / RATE)
dropouts = {}
for label, hz in TONES:
    if not present[hz]:
        print(f"{label}: missing")
        continue
    band = np.fft.rfft(mono)
    band[np.abs(all_freqs - hz) > 40.0] = 0
    tone = np.fft.irfft(band, len(mono))
    count = len(tone) // envelope_frames
    envelope = np.sqrt((tone[:count * envelope_frames].reshape(count, envelope_frames) ** 2).mean(axis=1))
    loud = np.flatnonzero(envelope > 0.5 * envelope.max())
    on, off = int(loud[0]), int(loud[-1])
    # 20 ms are skipped at each edge, where the tone's own fade looks like a dip.
    quiet = [i for i in range(on + 4, off - 3) if envelope[i] <= 0.5 * envelope.max()]
    gaps = []
    for i in quiet:
        if gaps and i == gaps[-1][1] + 1:
            gaps[-1][1] = i
        else:
            gaps.append([i, i])
    detail = "".join(f"; {a * 5 / 1000:.3f} s for {(b - a + 1) * 5} ms" for a, b in gaps)
    print(f"{label}: heard from {on * 5 / 1000:.3f} s to {(off + 1) * 5 / 1000:.3f} s, "
          f"{len(gaps)} dropout(s){detail}")
    dropouts[hz] = len(gaps)

if not all(present.values()):
    sys.exit(f"FAIL: a tone is below {MIN_RATIO:.0f}x the median spectral magnitude "
             f"or below {MIN_SHARE:.0%} of the other tone")
print(f"call-side dropouts: {dropouts[880.0]}")
if dropouts[880.0]:
    sys.exit(f"FAIL: the call side has {dropouts[880.0]} dropout(s)")
PY
)

BURST_PROBE=$(cat <<'PY'
import sys
import threading
import time

from recorder.backends.linux import ParecSource

RATE = 48000
SECONDS = 4.0

# The backend's own reader (parec arguments, unbuffered pipe, read size; see
# src/recorder/backends/linux.py) with no reserve, so every delivery is one parec read.
reads = []
errors = []
ended = threading.Event()


def on_end(error):
    errors.append(error)
    ended.set()


start = time.monotonic()
source = ParecSource(sys.argv[1])
source.start(lambda block: reads.append((time.monotonic() - start, len(block))), on_end)
ended.wait(SECONDS)
source.close()
ended.wait(5.0)
if errors and errors[0] is not None:
    sys.exit(f"{sys.argv[1]}: parec failed: {errors[0]}")
if len(reads) < 3:
    sys.exit(f"{sys.argv[1]}: only {len(reads)} reads in {SECONDS:.0f} s")
sizes = sorted(n for _, n in reads[1:-1] or reads)
gaps = sorted(b[0] - a[0] for a, b in zip(reads, reads[1:]))
print(f"{sys.argv[1]}: first audio after {reads[0][0]:.2f} s; {len(reads)} reads; frames per read "
      f"median {sizes[len(sizes) // 2]} (min {sizes[0]}, max {sizes[-1]}); interval median "
      f"{gaps[len(gaps) // 2] * 1000:.0f} ms (max {gaps[-1] * 1000:.0f} ms); "
      f"{sum(n for _, n in reads) / RATE:.2f} s of audio in {SECONDS:.0f} s")
PY
)

section() { printf '\n=== %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

host_main() {
    local repo
    repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    # docker run --rm only cleans up when the client survives; an interrupted run must not
    # leave the container behind.
    trap 'docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true' EXIT
    docker run --rm --init --name "$CONTAINER_NAME" -v "$repo:$SRC:ro" "$IMAGE" \
        bash "$SRC/scripts/e2e_linux.sh" --container
}

container_main() {
    export DEBIAN_FRONTEND=noninteractive
    section "installing pulseaudio, pulseaudio-utils, python3-venv, ffmpeg"
    if ! { apt-get update -qq && apt-get install -y -qq --no-install-recommends \
            ca-certificates pulseaudio pulseaudio-utils python3-venv ffmpeg; } >/tmp/apt.log 2>&1; then
        tail -40 /tmp/apt.log
        fail "apt-get install failed"
    fi

    useradd --create-home "$TESTER"
    mkdir -p "$WORK"
    # The host's virtualenvs hold macOS binaries, and audio/ and .state/ are host-local output.
    tar -C "$SRC" --exclude='./.venv*' --exclude='./.git' --exclude='./audio' --exclude='./.state' \
        --exclude='*/__pycache__' --exclude='./.pytest_cache' -cf - . | tar -C "$WORK" -xf -
    chown -R "$TESTER:$TESTER" "$WORK"

    # PulseAudio refuses to run a per-user server as root, and runuser keeps root's HOME.
    runuser -u "$TESTER" -- env HOME="/home/$TESTER" USER="$TESTER" LOGNAME="$TESTER" \
        bash "$SRC/scripts/e2e_linux.sh" --user
}

dump_diagnostics() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        section "diagnostics after failure (exit $status)"
        tail -30 "$XDG_RUNTIME_DIR/pulse.log" 2>/dev/null || true
        for log in /tmp/rec-*/.state/engine.log; do
            [[ -f $log ]] && { echo "--- $log"; cat "$log"; }
        done
    fi
}

wait_for_server() {
    local _
    for _ in $(seq 50); do
        pactl info >/dev/null 2>&1 && return 0
        sleep 0.2
    done
    cat "$XDG_RUNTIME_DIR/pulse.log"
    fail "PulseAudio did not start"
}

new_home() {
    mktemp -d /tmp/rec-XXXXXX
}

user_main() {
    export XDG_RUNTIME_DIR="/tmp/runtime-$TESTER"
    mkdir -m 700 "$XDG_RUNTIME_DIR"
    trap dump_diagnostics EXIT
    cd "$WORK"

    section "installing recorder and its test dependencies into a virtualenv"
    python3 --version
    python3 -m venv .venv
    .venv/bin/pip install -q --disable-pip-version-check -e ".[dev]"
    export PATH="$WORK/.venv/bin:$PATH"

    section "unit tests on Linux (pytest -q)"
    pytest -q || fail "the unit tests failed on Linux"

    section "starting PulseAudio as $(id -un) with no hardware"
    pulseaudio --version
    # -n skips default.pa, which would probe for ALSA/udev hardware a container does not have.
    pulseaudio -n --daemonize=no --exit-idle-time=-1 --log-target=stderr \
        --load=module-native-protocol-unix >"$XDG_RUNTIME_DIR/pulse.log" 2>&1 &
    wait_for_server

    section "pactl info with no sink and no source (PulseAudio prints '(null)' for a missing default)"
    LC_ALL=C pactl info
    section "recorder devices with no sink and no source"
    local empty_devices
    empty_devices="$(recorder devices)"
    echo "$empty_devices"
    grep -qF "Call loopback source : UNAVAILABLE (no default output device" <<<"$empty_devices" \
        || fail "devices does not report the missing default output"
    grep -qF "Microphone (your voice): UNAVAILABLE (no microphone" <<<"$empty_devices" \
        || fail "devices does not report the missing microphone"
    if grep -qF "(null)" <<<"$empty_devices"; then
        fail "devices took pactl's '(null)' placeholder for a device name"
    fi
    section "recorder start with no sink and no source"
    RECORDER_HOME="$(new_home)"
    export RECORDER_HOME
    local refused_empty
    if refused_empty="$(recorder start 2>&1)"; then
        echo "$refused_empty"
        recorder stop || true
        fail "recorder start succeeded with no sink and no source"
    fi
    echo "$refused_empty"
    grep -qF "no default output device" <<<"$refused_empty" \
        || fail "the refusal does not mention 'no default output device'"
    [[ "$(recorder status)" == "Not recording." ]] || fail "a recorder daemon is still running"
    recorder status

    section "loading null sink 'virt' (call) and virtual source 'fakemic' (microphone)"
    pactl load-module module-null-sink sink_name=virt >/dev/null
    pactl load-module module-null-sink sink_name=micsrc_sink >/dev/null
    pactl load-module module-virtual-source source_name=fakemic master=micsrc_sink.monitor >/dev/null
    pactl set-default-sink virt
    pactl set-default-source fakemic

    section "pactl info"
    LC_ALL=C pactl info
    section "pactl list short sinks"
    pactl list short sinks
    section "pactl list short sources"
    pactl list short sources
    section "recorder devices"
    local devices
    devices="$(recorder devices)"
    echo "$devices"
    grep -q "^Call loopback source : virt.monitor @ 48000 Hz$" <<<"$devices" \
        || fail "devices does not name virt.monitor as the call loopback"
    grep -q "^Microphone (your voice): fakemic @ 48000 Hz$" <<<"$devices" \
        || fail "devices does not name fakemic as the microphone"

    section "scenario 1: record 880 Hz on the call and 440 Hz on the microphone"
    local tones
    tones="$(mktemp -d)"
    ffmpeg -v error -f lavfi -i "sine=frequency=880:duration=6" -ar 48000 -ac 2 "$tones/call.wav"
    ffmpeg -v error -f lavfi -i "sine=frequency=440:duration=6" -ar 48000 -ac 2 "$tones/voice.wav"
    RECORDER_HOME="$(new_home)"
    export RECORDER_HOME
    recorder start || fail "recorder start failed"
    local call_pid voice_pid
    paplay --device=virt "$tones/call.wav" &
    call_pid=$!
    paplay --device=micsrc_sink "$tones/voice.wav" &
    voice_pid=$!
    wait "$call_pid" || fail "paplay into virt failed"
    wait "$voice_pid" || fail "paplay into micsrc_sink failed"
    sleep 1
    local stopped
    stopped="$(recorder stop 2>&1)" || { echo "$stopped"; fail "recorder stop failed"; }
    echo "$stopped"
    grep -q "^Saved " <<<"$stopped" || fail "recorder stop did not print Saved"
    if grep -q "Warning:" <<<"$stopped"; then
        fail "recorder stop printed a warning"
    fi
    echo "--- engine.log"
    cat "$RECORDER_HOME/.state/engine.log"

    local mp3s=("$RECORDER_HOME"/audio/*.mp3)
    [[ ${#mp3s[@]} -eq 1 && -f ${mp3s[0]} ]] || fail "expected exactly one mp3, found: ${mp3s[*]}"
    section "FFT of ${mp3s[0]##*/}"
    python -u -c "$TONE_CHECK" "${mp3s[0]}"

    section "scenario 2: default source is a monitor (echo guard)"
    pactl set-default-source virt.monitor
    LC_ALL=C pactl info | grep -E '^Default (Sink|Source):'
    recorder devices
    RECORDER_HOME="$(new_home)"
    export RECORDER_HOME
    local refused
    if refused="$(recorder start 2>&1)"; then
        echo "$refused"
        recorder stop || true
        fail "recorder start succeeded although the default source is a monitor"
    fi
    echo "$refused"
    grep -q "no microphone" <<<"$refused" || fail "the refusal does not mention 'no microphone'"
    [[ "$(recorder status)" == "Not recording." ]] || fail "a recorder daemon is still running"
    recorder status

    section "parec delivery with the backend's arguments (informational)"
    python -u -c "$BURST_PROBE" virt.monitor
    python -u -c "$BURST_PROBE" fakemic

    section "PASS: unit tests green, missing defaults reported, both tones recorded with no call-side dropout, echo guard refused a monitor"
}

case "${1:-}" in
    "") host_main ;;
    --container) container_main ;;
    --user) user_main ;;
    *) fail "unknown argument: $1" ;;
esac
