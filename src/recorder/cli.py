"""The ``recorder`` command: start/stop/status/devices subcommands.

Kept free of audio-library imports so ``status``/``stop`` run instantly; the daemon process
(and the ``devices`` listing) pull in soundcard on demand.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import state
from .config import BLOCK, SAMPLERATE

_STARTUP_GRACE_SECONDS = 1.2
_STOP_TIMEOUT_SECONDS = 15.0


def cmd_start(_args) -> int:
    """Start a background recording, refusing if one is already running."""
    if state.is_active():
        existing = state.read_state() or {}
        print(f"Already recording since {existing.get('started_at', '?')} (pid {existing.get('pid', '?')}).")
        return 1

    state.ensure_dirs()
    state.clear_stop_flag()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = state.audio_dir() / f"recording-{timestamp}.mp3"

    creationflags = 0
    kwargs = {}
    if os.name == "nt":
        creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-m", "recorder._daemon",
         "--output", str(output),
         "--samplerate", str(SAMPLERATE),
         "--blocksize", str(BLOCK)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        **kwargs,
    )
    state.write_state({"pid": proc.pid, "started_at": timestamp, "output": str(output)})

    time.sleep(_STARTUP_GRACE_SECONDS)
    if not state.pid_alive(proc.pid):
        print("Recording failed to start. Last log lines:")
        print(state.tail_log())
        state.clear_state()
        return 1

    print(f"Recording started (pid {proc.pid}) -> {output}")
    return 0


def cmd_stop(_args) -> int:
    """Signal the daemon to finish, wait for the mp3, and report where it landed.

    If the daemon dies or has to be killed before publishing the mp3, the audio it already
    streamed to disk is published instead of discarded.
    """
    data = state.read_state()
    if not data or not state.pid_alive(int(data.get("pid", 0))):
        print("No active recording.")
        state.clear_state()
        return 1

    output = Path(data["output"])
    state.set_stop_flag()

    deadline = time.time() + _STOP_TIMEOUT_SECONDS
    while time.time() < deadline:
        if not state.pid_alive(int(data["pid"])):
            break
        time.sleep(0.3)

    if state.pid_alive(int(data["pid"])):
        state.terminate(int(data["pid"]))
    state.clear_stop_flag()
    state.clear_state()
    recovered = state.publish_partial(output)

    if output.exists():
        size_mb = output.stat().st_size / 1_000_000
        print(f"Saved {output} ({size_mb:.1f} MB)")
        if recovered:
            print("The recorder did not shut down cleanly; the audio it had written was recovered "
                  "(the last moment may be cut). Details in .state/engine.log.")
        return 0
    print("Stop signalled, but no mp3 was produced — likely no audio reached the devices.")
    print("Last log lines:")
    print(state.tail_log())
    return 1


def cmd_status(_args) -> int:
    """Print whether a recording is in progress."""
    data = state.read_state()
    if data and state.pid_alive(int(data.get("pid", 0))):
        print(f"Recording since {data['started_at']} (pid {data['pid']}) -> {data['output']}")
    else:
        print("Not recording.")
    return 0


def cmd_devices(_args) -> int:
    """List the loopback and microphone devices that would be captured."""
    from .audio_io import describe_devices, open_pyaudio

    pa = open_pyaudio()
    try:
        print(describe_devices(pa))
    finally:
        pa.terminate()
    return 0


def main(argv=None) -> int:
    """Parse arguments and dispatch to the selected subcommand. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="recorder", description="Record the call + your mic to mp3, with no echo."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="Start recording the call + your mic to mp3")
    subparsers.add_parser("stop", help="Stop recording and save the mp3")
    subparsers.add_parser("status", help="Show whether a recording is in progress")
    subparsers.add_parser("devices", help="List capture devices (debug wrong-device silence)")
    args = parser.parse_args(argv)

    handlers = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "devices": cmd_devices}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
