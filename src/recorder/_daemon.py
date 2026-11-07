"""Detached background entry point: capture until the stop sentinel, then save the mp3.

Launched by ``recorder start`` as ``python -m recorder._daemon``. It has no console, so all
diagnostics go to ``.state/engine.log`` — the only way to see why a recording failed.
"""

from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime
from pathlib import Path

from . import state
from .config import BLOCK, SAMPLERATE


def _logger():
    """Return a function that appends timestamped lines to the daemon log."""
    path = state.log_file()

    def log(message: str) -> None:
        """Append one timestamped ``message`` line to the daemon log."""
        stamp = datetime.now().strftime("%H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")

    return log


def _watch_for_stop(stop_event: threading.Event) -> None:
    """Set ``stop_event`` as soon as ``recorder stop`` drops the sentinel file."""
    while not stop_event.is_set():
        if state.stop_requested():
            stop_event.set()
            return
        time.sleep(0.2)


def main(argv=None) -> int:
    """Run one recording session to completion. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="recorder-daemon")
    parser.add_argument("--output", required=True)
    parser.add_argument("--samplerate", type=int, default=SAMPLERATE)
    parser.add_argument("--blocksize", type=int, default=BLOCK)
    args = parser.parse_args(argv)

    from .engine import record  # deferred so audio libs load only in the daemon

    log = _logger()
    output = Path(args.output)
    stop_event = threading.Event()
    watcher = threading.Thread(target=_watch_for_stop, args=(stop_event,), daemon=True)
    watcher.start()

    log(f"daemon start -> {output}")
    try:
        record(output, stop_event, log, args.samplerate, args.blocksize)
        return 0
    except Exception as exc:  # noqa: BLE001 - last-resort guard; detail goes to the log
        log(f"FATAL {exc!r}")
        if state.publish_partial(output):
            log(f"kept the audio captured before the failure in {output}")
        return 1
    finally:
        state.clear_stop_flag()
        state.clear_state()


if __name__ == "__main__":
    raise SystemExit(main())
