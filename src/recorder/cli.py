"""The ``recorder`` command: start/stop/status/devices/transcribe subcommands.

Kept free of audio-library imports so ``status``/``stop`` run instantly; the daemon process
(and the ``devices`` listing) pull in the platform's audio library on demand, and
``transcribe`` loads its decoder and OpenAI client only once it runs.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import state
from .backends import select_backend
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
    state.clear_warning()
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
    # The child is ours, so its own exit status is authoritative; probing the pid would reap it
    # behind Popen's back.
    if proc.poll() is not None:
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
    # Only after publishing: the warning is a side note and must never cost the recording. It
    # is still consumed here, so it cannot leak into a later stop.
    warning = state.pop_warning()

    if output.exists():
        size_mb = output.stat().st_size / 1_000_000
        print(f"Saved {output} ({size_mb:.1f} MB)")
        if warning:
            print(f"Warning: {warning}")
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
    backend = select_backend()
    try:
        print(backend.describe())
    finally:
        backend.close()
    return 0


def _missing_extras() -> List[str]:
    """Return the libraries of the optional ``transcribe`` extra that are not installed."""
    return [name for name in ("openai", "miniaudio") if importlib.util.find_spec(name) is None]


def _resolve_input(path_arg: Optional[str]) -> Path:
    """Return the recording to transcribe: ``path_arg``, else the newest mp3 in ``audio/``.

    An in-progress ``.mp3.part`` never matches, so a running recording is not picked up.

    Raises:
        ValueError: With a user-facing message if the path is missing, not a file, or no
            finished mp3 exists.
    """
    if path_arg:
        path = Path(path_arg)
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {path}")
        return path
    newest = max(state.audio_dir().glob("*.mp3"), key=lambda p: p.stat().st_mtime, default=None)
    if newest is None:
        raise ValueError(
            f"No mp3 found in {state.audio_dir()}. Record one with `recorder start` and "
            "`recorder stop`, or pass a path."
        )
    return newest


def _parse_list(value: Optional[str]) -> List[str]:
    """Split a comma-separated flag into stripped, non-empty items."""
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


def _clock(seconds: float) -> str:
    """Format ``seconds`` as ``minutes:seconds``; minutes may exceed 59 for long recordings."""
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _report_transcription_failure(error, output: Path) -> int:
    """Save what was transcribed before ``error``, explain the failure, and pick the exit code."""
    from . import transcribe

    partial = output.with_name(output.name + ".partial")
    text = transcribe.join_texts(error.completed)
    if text:
        partial.write_text(text, encoding="utf-8")
    where = f"chunk {error.chunk_index + 1} ({_clock(error.start_s)} into the recording)"
    interrupted = isinstance(error.cause, KeyboardInterrupt)
    if interrupted:
        print(f"Interrupted at {where}.")
    elif getattr(error.cause, "status_code", None) == 401:
        # The API's own message echoes part of the key, so it is deliberately not printed.
        print(f"The API rejected the key at {where}. Check OPENAI_API_KEY.")
    else:
        print(f"Transcription failed at {where}: {error.cause}")
    if text:
        print(f"The {len(error.completed)} finished chunk(s) are saved in {partial}")
    return 130 if interrupted else 1


def cmd_transcribe(args) -> int:
    """Transcribe a recording to a plain-text file next to it with OpenAI.

    Everything that can be checked cheaply (input, overwrite, key, libraries, keywords) is
    checked before any audio is decoded or any request is billed.
    """
    try:
        path = _resolve_input(args.path)
    except ValueError as exc:
        print(exc)
        return 1
    output = path.with_suffix(".txt")
    if output.exists() and not args.force:
        print(f"{output} already exists. Pass --force to overwrite it.")
        return 1
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("OPENAI_API_KEY is not set. Export it first, for example: export OPENAI_API_KEY=sk-...")
        return 1
    missing = _missing_extras()
    if missing:
        print(f"Missing {', '.join(missing)}. Install them with: pip install -e \".[transcribe]\"")
        return 1
    languages = _parse_list(args.languages)
    keywords = _parse_list(args.keywords)
    for keyword in keywords:
        if any(char in keyword for char in "<>\r\n"):
            print(f"Invalid keyword {keyword!r}: keywords may not contain <, > or line breaks.")
            return 1

    from . import chunker, transcribe

    def show_progress(done: int, audio_seconds: float) -> None:
        """Print one line per finished chunk."""
        print(f"chunk {done} done ({_clock(audio_seconds)} of audio)", flush=True)

    print(f"Transcribing {path} ...", flush=True)
    try:
        texts = transcribe.transcribe_chunks(
            chunker.iter_chunks(path),
            transcribe.make_client(),
            languages=languages,
            keywords=keywords,
            on_progress=show_progress,
        )
    except transcribe.TranscriptionError as exc:
        return _report_transcription_failure(exc, output)

    transcript = transcribe.join_texts(texts)
    if not transcript:
        print("No speech detected, so nothing was written. If this recording should have "
              "speech, run `recorder devices` to check the capture devices.")
        return 1
    output.write_text(transcript, encoding="utf-8")
    chunk_word = "chunk" if len(texts) == 1 else "chunks"
    print(f"Saved {output} ({len(transcript)} characters, {len(texts)} {chunk_word})")
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
    transcribe_parser = subparsers.add_parser(
        "transcribe", help="Transcribe a recording to a text file with OpenAI"
    )
    transcribe_parser.add_argument(
        "path", nargs="?", help="mp3 to transcribe (default: the newest in audio/)"
    )
    transcribe_parser.add_argument(
        "--languages", help="comma-separated language hints such as it,en (default: auto-detect)"
    )
    transcribe_parser.add_argument(
        "--keywords", help="comma-separated names or terms likely to be spoken"
    )
    transcribe_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing transcript"
    )
    args = parser.parse_args(argv)

    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "devices": cmd_devices,
        "transcribe": cmd_transcribe,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
