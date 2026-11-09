"""Tests for the filesystem state coordinating the start/stop/daemon processes."""

import contextlib
import os
import select
import signal
import subprocess
import sys
import time

import pytest

from recorder import state

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-control semantics")

_SLEEPER = "import time; time.sleep(60)"
_GROUP_PARENT = """
import subprocess, time
child = subprocess.Popen(["sleep", "60"])
print(child.pid, flush=True)
time.sleep(60)
"""
_TERM_IGNORER = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point the state/audio directories at a throwaway folder for every test."""
    monkeypatch.setenv("RECORDER_HOME", str(tmp_path))
    return tmp_path


def test_state_roundtrip():
    """Written state reads back; clearing removes it."""
    state.write_state({"pid": 1234, "started_at": "now", "output": "x.mp3"})
    assert state.read_state()["pid"] == 1234
    state.clear_state()
    assert state.read_state() is None


def test_read_missing_state_is_none():
    """Absent state is reported as None, not an error."""
    assert state.read_state() is None


def test_stop_flag_lifecycle():
    """The stop sentinel can be set, observed, and cleared."""
    assert not state.stop_requested()
    state.set_stop_flag()
    assert state.stop_requested()
    state.clear_stop_flag()
    assert not state.stop_requested()


def test_pid_alive_for_self_and_bogus():
    """The current process is alive; an absurd pid and zero are not."""
    assert state.pid_alive(os.getpid())
    assert not state.pid_alive(0)
    assert not state.pid_alive(2_000_000_000)


def test_is_active_clears_stale_state():
    """State naming a dead pid is treated as stale and removed."""
    state.write_state({"pid": 2_000_000_000, "started_at": "old", "output": "x.mp3"})
    assert state.is_active() is False
    assert state.read_state() is None


def test_is_active_true_for_live_pid():
    """State naming a live pid counts as an active recording."""
    state.write_state({"pid": os.getpid(), "started_at": "now", "output": "x.mp3"})
    assert state.is_active() is True


def test_warning_roundtrip_and_pop_deletes():
    """A written warning is handed out exactly once; clearing an absent one is harmless."""
    assert state.pop_warning() is None
    state.write_warning("grant System Audio Recording \u2014 then retry")
    assert state.warning_file().exists()
    assert state.pop_warning() == "grant System Audio Recording \u2014 then retry"
    assert state.pop_warning() is None
    assert not state.warning_file().exists()
    state.clear_warning()
    state.write_warning("again")
    state.clear_warning()
    assert state.pop_warning() is None


def test_pop_warning_treats_an_undecodable_note_as_none_and_deletes_it():
    """A corrupt note is not worth an exception in ``stop``; it is dropped instead."""
    state.ensure_dirs()
    state.warning_file().write_bytes(b"\xff\xfe broken \x80")
    assert state.pop_warning() is None
    assert not state.warning_file().exists()


def test_pop_warning_treats_an_unreadable_path_as_none():
    """Something other than a file at the warning path reads as no warning instead of raising."""
    state.ensure_dirs()
    state.warning_file().mkdir()
    assert state.pop_warning() is None


def test_tail_log_without_file():
    """Tailing a missing log returns a placeholder rather than raising."""
    assert state.tail_log() == "(no engine.log)"


@pytest.fixture
def procs():
    """Collect spawned processes and make sure none outlives its test."""
    spawned = []
    yield spawned
    for proc in spawned:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


def _spawn(procs, code, **kwargs):
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, **kwargs)
    procs.append(proc)
    return proc


def _read_line(proc, timeout=10.0):
    """Return the first stdout line of ``proc``, failing instead of blocking past ``timeout``."""
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    data = b""
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "child never printed a line"
        if select.select([fd], [], [], remaining)[0]:
            chunk = os.read(fd, 64)
            assert chunk, "child closed stdout without printing a line"
            data += chunk
    return data.split(b"\n", 1)[0].decode()


def _eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@posix_only
def test_pid_alive_is_true_when_signalling_is_not_permitted(monkeypatch):
    """A pid owned by another user still counts as alive when signal 0 is refused."""

    def refuse(*_args):
        raise PermissionError

    def not_our_child(*_args):
        raise ChildProcessError

    monkeypatch.setattr(state.os, "kill", refuse)
    monkeypatch.setattr(state.os, "waitpid", not_our_child)
    assert state.pid_alive(4242)


@posix_only
def test_pid_alive_is_false_for_an_exited_child(procs):
    """An exited-but-unreaped child is not alive, although it still answers signal 0."""
    proc = _spawn(procs, "pass")
    assert _eventually(lambda: not state.pid_alive(proc.pid))


@posix_only
def test_terminate_kills_the_whole_process_group(procs):
    """A process that leads its own group takes its children down with it."""
    parent = _spawn(procs, _GROUP_PARENT, start_new_session=True)
    child_pid = int(_read_line(parent))
    try:
        state.terminate(parent.pid)
        parent.wait(timeout=5)
        assert _eventually(lambda: _pid_gone(child_pid))
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)


@posix_only
def test_terminate_does_not_kill_a_foreign_group(procs):
    """A process sharing the caller's group is signalled alone, never via its group."""
    sleeper = _spawn(procs, _SLEEPER)
    assert os.getpgid(sleeper.pid) != sleeper.pid
    state.terminate(sleeper.pid)
    sleeper.wait(timeout=5)
    assert state.pid_alive(os.getpid())


@posix_only
def test_terminate_escalates_to_sigkill(procs, monkeypatch):
    """A process that ignores SIGTERM is killed once the grace period runs out."""
    monkeypatch.setattr(state, "_TERM_GRACE_SECONDS", 0.3)
    sent = []
    real_signal = state._signal

    def recording_signal(pid, sig):
        sent.append(sig)
        real_signal(pid, sig)

    monkeypatch.setattr(state, "_signal", recording_signal)
    stubborn = _spawn(procs, _TERM_IGNORER)
    assert _read_line(stubborn) == "ready"
    state.terminate(stubborn.pid)
    stubborn.wait(timeout=5)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
