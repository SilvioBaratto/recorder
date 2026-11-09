"""Filesystem state shared between the `start`, `stop`, and daemon processes.

Everything the three processes need to coordinate lives under ``<root>/.state``: the active
recording's metadata, the stop sentinel, and the daemon log. Output mp3s live under
``<root>/audio``. ``<root>`` defaults to this project folder and can be overridden with the
``RECORDER_HOME`` environment variable.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_UNLOCK_TIMEOUT_SECONDS = 5.0
_TERM_GRACE_SECONDS = 2.0


def root() -> Path:
    """Return the project root that anchors the audio and state directories."""
    override = os.environ.get("RECORDER_HOME")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2]


def audio_dir() -> Path:
    """Return the directory where finished mp3s are written."""
    return root() / "audio"


def state_dir() -> Path:
    """Return the directory holding the state file, stop sentinel, and log."""
    return root() / ".state"


def _state_file() -> Path:
    return state_dir() / "recording.json"


def _stop_flag() -> Path:
    return state_dir() / "stop.flag"


def log_file() -> Path:
    """Return the daemon log path (the only window into the detached process)."""
    return state_dir() / "engine.log"


def warning_file() -> Path:
    """Return the path where the daemon leaves a note for ``recorder stop`` to print."""
    return state_dir() / "warning.txt"


def partial_path(output: Path) -> Path:
    """Return the in-progress sibling the daemon streams mp3 into before publishing ``output``."""
    return output.with_name(output.name + ".part")


def publish_partial(output: Path) -> bool:
    """Rename ``output``'s ``.part`` to ``output``, or delete it if nothing was written.

    Safe to call when a recording ended badly: mp3 is a run of self-contained frames, so a
    ``.part`` cut off mid-write still plays.

    Args:
        output: Final mp3 path whose ``.part`` sibling should be published.

    Returns:
        Whether a non-empty ``.part`` was published as ``output``.

    Raises:
        PermissionError: If the ``.part`` is still locked after a few seconds.
    """
    partial = partial_path(output)
    deadline = time.monotonic() + _UNLOCK_TIMEOUT_SECONDS
    while True:
        try:
            if not partial.exists():
                return False
            if partial.stat().st_size == 0:
                partial.unlink()
                return False
            partial.replace(output)
            return True
        except PermissionError:
            # Windows keeps a dead or killed daemon's handle on the file for a few milliseconds.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def ensure_dirs() -> None:
    """Create the audio and state directories if they do not exist."""
    audio_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)


def write_state(data: dict) -> None:
    """Persist the active recording's metadata (pid, start time, output path)."""
    ensure_dirs()
    _state_file().write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_state() -> dict | None:
    """Return the active recording's metadata, or None if the file is absent/corrupt."""
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_state() -> None:
    """Remove the state file, ignoring absence."""
    _state_file().unlink(missing_ok=True)


def write_warning(message: str) -> None:
    """Leave ``message`` for the next ``recorder stop``, which runs in another process."""
    ensure_dirs()
    warning_file().write_text(message, encoding="utf-8")


def pop_warning() -> Optional[str]:
    """Return the pending warning and delete it, or None if there is none."""
    try:
        message = warning_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    warning_file().unlink(missing_ok=True)
    return message


def clear_warning() -> None:
    """Remove any pending warning, ignoring absence."""
    warning_file().unlink(missing_ok=True)


def set_stop_flag() -> None:
    """Create the sentinel that tells the daemon to finalize and exit."""
    ensure_dirs()
    _stop_flag().write_text(datetime.now().isoformat(), encoding="utf-8")


def stop_requested() -> bool:
    """Return whether the stop sentinel is present."""
    return _stop_flag().exists()


def clear_stop_flag() -> None:
    """Remove the stop sentinel, ignoring absence."""
    _stop_flag().unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """Return whether the process with ``pid`` is currently running.

    Uses ``OpenProcess``/``GetExitCodeProcess`` on Windows (``os.kill`` does not support a
    liveness probe there). On POSIX an exited child of the caller is reaped and reported dead,
    and any other pid is probed with signal 0; a pid we may not signal still counts as alive.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        # An exited but unreaped child is a zombie that still answers signal 0, so reap it first.
        if os.waitpid(pid, os.WNOHANG)[0] != 0:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pid: int, sig: int) -> None:
    """Send ``sig`` to ``pid`` and, when it leads its own process group, to that whole group.

    A process that already exited or belongs to another user is ignored.
    """
    try:
        # killpg on a pid that is not a group leader would signal a different group, possibly
        # the caller's own, so only a leader is signalled as a group.
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate(pid: int) -> None:
    """Terminate a process (and its children) by pid, best-effort.

    Windows kills the process tree outright. On POSIX the process (or its whole group, when it
    leads one) gets ``SIGTERM`` and a short grace period, then ``SIGKILL`` if it is still alive.
    """
    if not pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        return
    _signal(pid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.05)
    if pid_alive(pid):
        _signal(pid, signal.SIGKILL)


def is_active() -> bool:
    """Return whether a live recording daemon is running.

    A state file naming a dead pid is treated as stale and cleared, so a crashed daemon does
    not block the next ``start``.
    """
    data = read_state()
    if not data:
        return False
    if pid_alive(int(data.get("pid", 0))):
        return True
    clear_state()
    return False


def tail_log(lines: int = 15) -> str:
    """Return the last ``lines`` of the daemon log, or a placeholder if there is none."""
    try:
        content = log_file().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return "(no engine.log)"
    return "\n".join(content[-lines:]) if content else "(engine.log empty)"
