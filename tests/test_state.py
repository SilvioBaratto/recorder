"""Tests for the filesystem state coordinating the start/stop/daemon processes."""

import os

import pytest

from recorder import state


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


def test_tail_log_without_file():
    """Tailing a missing log returns a placeholder rather than raising."""
    assert state.tail_log() == "(no engine.log)"
