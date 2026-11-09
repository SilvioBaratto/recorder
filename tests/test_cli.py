"""Tests for argument dispatch and the no-audio CLI paths."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fake_backend import FakeBackend
from recorder import cli, state

_FAKE_DAEMON = """
import os, sys, time
from recorder import state
part, payload, mode = sys.argv[1], sys.argv[2].encode(), sys.argv[3]
with open(part, "wb") as handle:
    handle.write(payload)
    handle.flush()
    while mode == "hang" or not state.stop_requested():
        time.sleep(0.05)
    os._exit(3)
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Isolate state so CLI tests never touch a real recording."""
    monkeypatch.setenv("RECORDER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_daemon(isolated_home):
    """Return a starter for a stand-in daemon that writes the .part but never publishes it.

    The stand-in holds the .part open like the real daemon. By default it dies the moment stop
    is signalled, as the real one did when PortAudio teardown crashed; with ``hang=True`` it
    ignores stop, so ``recorder stop`` has to kill it.
    """
    procs = []

    def start(payload: bytes, hang: bool = False) -> Path:
        """Launch the stand-in recording ``payload`` and register it as the active recording."""
        state.ensure_dirs()
        output = isolated_home / "audio" / "rec.mp3"
        part = isolated_home / "audio" / "rec.mp3.part"
        proc = subprocess.Popen([sys.executable, "-c", _FAKE_DAEMON, str(part), payload.decode(),
                                 "hang" if hang else "die"])
        procs.append(proc)
        deadline = time.monotonic() + 10
        while not (part.exists() and part.stat().st_size == len(payload)):
            assert time.monotonic() < deadline, "stand-in daemon never wrote its .part"
            time.sleep(0.02)
        state.write_state({"pid": proc.pid, "started_at": "now", "output": str(output)})
        return output

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


def test_requires_a_subcommand():
    """Bare invocation errors out instead of doing something surprising."""
    with pytest.raises(SystemExit):
        cli.main([])


def test_main_dispatches_to_handler(monkeypatch):
    """Each subcommand routes to its handler and returns that handler's code."""
    calls = []
    for name in ("cmd_start", "cmd_stop", "cmd_status", "cmd_devices"):
        monkeypatch.setattr(cli, name, lambda _a, _n=name: calls.append(_n) or 0)
    assert cli.main(["status"]) == 0
    assert calls == ["cmd_status"]


def test_status_reports_idle():
    """With no state, status prints idle and succeeds."""
    assert cli.cmd_status(None) == 0


def test_stop_without_recording_is_an_error():
    """Stopping when nothing is recording is a clean, non-zero outcome."""
    assert cli.cmd_stop(None) == 1


def test_start_refuses_when_already_active(monkeypatch):
    """A second start is refused while a live recording is tracked."""
    state.write_state({"pid": __import__("os").getpid(), "started_at": "now", "output": "x.mp3"})
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn a daemon")
    )
    assert cli.cmd_start(None) == 1


def test_stop_recovers_audio_when_daemon_dies_before_saving(fake_daemon):
    """A daemon that dies on stop without publishing still leaves the user their mp3."""
    output = fake_daemon(b"ID3 captured frames")
    assert cli.cmd_stop(None) == 0
    assert output.read_bytes() == b"ID3 captured frames"
    assert not Path(str(output) + ".part").exists()


def test_stop_recovers_audio_when_daemon_must_be_killed(fake_daemon, monkeypatch):
    """A daemon that ignores stop is killed, and the audio it already wrote is published."""
    monkeypatch.setattr(cli, "_STOP_TIMEOUT_SECONDS", 0.5)
    output = fake_daemon(b"ID3 captured frames", hang=True)
    assert cli.cmd_stop(None) == 0
    assert output.read_bytes() == b"ID3 captured frames"


def test_devices_prints_backend_description(capsys, monkeypatch):
    """``recorder devices`` prints whatever the platform backend reports, then releases it."""
    backend = FakeBackend()
    backend.describe = lambda: "X"
    monkeypatch.setattr(cli, "select_backend", lambda: backend)
    assert cli.cmd_devices(None) == 0
    assert "X" in capsys.readouterr().out
    assert backend.closed


def test_stop_prints_a_pending_warning(fake_daemon, capsys):
    """A warning the daemon left behind is shown after the saved line, then consumed."""
    fake_daemon(b"ID3 captured frames")
    state.write_warning("call side silent")
    assert cli.cmd_stop(None) == 0
    out = capsys.readouterr().out
    assert out.index("Saved") < out.index("Warning: call side silent")
    assert state.pop_warning() is None


def test_stop_discards_the_warning_when_no_mp3_was_produced(fake_daemon, capsys):
    """A warning about a recording that does not exist is dropped rather than shown."""
    fake_daemon(b"")
    state.write_warning("call side silent")
    assert cli.cmd_stop(None) == 1
    assert "call side silent" not in capsys.readouterr().out
    assert state.pop_warning() is None


def test_start_clears_a_stale_warning(monkeypatch):
    """A warning from an earlier recording must not leak into a new one."""

    class _Healthy:
        pid = os.getpid()

        def poll(self):
            """Report the daemon as still running."""
            return None

    state.write_warning("left over from the last recording")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_k: _Healthy())
    monkeypatch.setattr(cli, "_STARTUP_GRACE_SECONDS", 0.01)
    try:
        assert cli.cmd_start(None) == 0
        assert state.pop_warning() is None
    finally:
        state.clear_state()


def test_stop_does_not_publish_an_empty_recording(fake_daemon):
    """An empty .part means nothing was encoded: no mp3, and stop reports failure."""
    output = fake_daemon(b"")
    assert cli.cmd_stop(None) == 1
    assert not output.exists()
    assert not Path(str(output) + ".part").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX zombie semantics")
def test_start_reports_a_child_that_exits_immediately(monkeypatch, capsys):
    """A daemon that dies during the startup grace is reported as failed and leaves no state."""
    real_popen = subprocess.Popen
    spawned = []

    def exits_immediately(_cmd, **kwargs):
        proc = real_popen([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", exits_immediately)
    monkeypatch.setattr(cli, "_STARTUP_GRACE_SECONDS", 0.5)
    try:
        assert cli.cmd_start(None) == 1
        assert state.read_state() is None
        assert "failed to start" in capsys.readouterr().out
    finally:
        for proc in spawned:
            proc.wait()
