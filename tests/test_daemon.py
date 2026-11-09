"""End-to-end daemon runs against fake devices: a recording must survive its own shutdown."""

from __future__ import annotations

import threading
import time

import pytest

from fake_backend import FakeBackend, FakeSource
from fake_pyaudio import MIC, RATE, FakePyAudio, FakePyAudioModule, FakeWindowsBackend, TeardownCrash
from recorder import _daemon, engine, state


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point the state/audio directories at a throwaway folder for every test."""
    monkeypatch.setenv("RECORDER_HOME", str(tmp_path))
    state.ensure_dirs()
    return tmp_path


@pytest.fixture
def devices(monkeypatch):
    """Return a factory that wires a :class:`FakePyAudio` into the engine's Windows backend."""
    created = []

    def install(**kwargs) -> FakePyAudio:
        """Build a FakePyAudio from ``kwargs`` and make the engine capture through it."""
        pa = FakePyAudio(**kwargs)
        backend = FakeWindowsBackend(FakePyAudioModule(pa))
        monkeypatch.setattr(engine, "select_backend", lambda: backend)
        created.append(pa)
        return pa

    yield install
    for pa in created:
        pa.release_all()


def _record_one_second_then_stop(pa: FakePyAudio, output) -> BaseException | None:
    """Run the daemon until the mic has delivered a second of audio, then signal stop.

    Returns:
        What the daemon died with, or None if it exited normally.
    """
    outcome = {}

    def run():
        """Run the daemon, capturing whatever it dies with."""
        try:
            _daemon.main(["--output", str(output)])
        except BaseException as exc:  # noqa: BLE001 - handed to the test to assert on
            outcome["exc"] = exc

    daemon = threading.Thread(target=run, daemon=True)
    daemon.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        mic = pa.streams.get(MIC["index"])
        if mic is not None and mic.frames_delivered >= RATE:
            break
        time.sleep(0.01)
    state.set_stop_flag()
    daemon.join(timeout=20)
    assert not daemon.is_alive(), "daemon did not exit after stop"
    return outcome.get("exc")


def test_stop_during_call_silence_shuts_down_cleanly_and_saves(devices, tmp_path):
    """Stopping while nothing plays (the loopback has no data) exits cleanly with the mp3."""
    pa = devices()
    output = tmp_path / "audio" / "rec.mp3"
    assert _record_one_second_then_stop(pa, output) is None
    assert output.exists() and output.stat().st_size > 0
    assert not (tmp_path / "audio" / "rec.mp3.part").exists()


def test_mp3_is_saved_before_audio_teardown(devices, tmp_path):
    """A crash while tearing PortAudio down cannot take the finished recording with it."""
    pa = devices(crash_on_terminate=True)
    output = tmp_path / "audio" / "rec.mp3"
    assert isinstance(_record_one_second_then_stop(pa, output), TeardownCrash)
    assert output.exists() and output.stat().st_size > 0


def test_opening_failure_closes_already_opened_sources(monkeypatch, tmp_path):
    """If the mic cannot open, the loopback already opened is released along with the backend."""
    backend = FakeBackend(fail_microphone=True)
    monkeypatch.setattr(engine, "select_backend", lambda: backend)
    with pytest.raises(RuntimeError, match="failed to open"):
        engine.record(tmp_path / "audio" / "rec.mp3", threading.Event(), lambda _msg: None,
                      RATE, RATE // 10)
    assert backend.loopback.closed
    assert backend.closed


def _log_failing_on(prefix: str):
    """Return a log callable that raises on the line starting with ``prefix``."""

    def log(message: str) -> None:
        """Fail like a log sink that broke mid-start."""
        if message.startswith(prefix):
            raise OSError("log sink broke")

    return log


def test_failure_after_both_sources_opened_closes_them(monkeypatch, tmp_path):
    """Opened sources are released even when the step after opening them fails.

    catap's worker thread is not a daemon thread, so an unclosed tap would keep the daemon
    process alive after it already cleared the state file.
    """
    backend = FakeBackend()
    monkeypatch.setattr(engine, "select_backend", lambda: backend)
    with pytest.raises(OSError, match="log sink broke"):
        engine.record(tmp_path / "audio" / "rec.mp3", threading.Event(),
                      _log_failing_on("microphone ="), RATE, RATE // 10)
    assert backend.loopback.closed
    assert backend.microphone.closed
    assert not backend.loopback.started and not backend.microphone.started
    assert backend.closed


def test_failure_after_the_loopback_opened_closes_it(monkeypatch, tmp_path):
    """A failure right after the loopback opened releases it before the microphone is touched."""
    backend = FakeBackend()
    monkeypatch.setattr(engine, "select_backend", lambda: backend)
    with pytest.raises(OSError, match="log sink broke"):
        engine.record(tmp_path / "audio" / "rec.mp3", threading.Event(),
                      _log_failing_on("loopback ="), RATE, RATE // 10)
    assert backend.loopback.closed
    assert not backend.microphone.closed
    assert backend.closed


def _run_daemon_for_a_second(backend: FakeBackend, monkeypatch, output) -> None:
    """Run the daemon against ``backend`` until its mic has pushed a second of audio, then stop."""
    monkeypatch.setattr(engine, "select_backend", lambda: backend)
    daemon = threading.Thread(target=_daemon.main, args=(["--output", str(output)],), daemon=True)
    daemon.start()
    deadline = time.monotonic() + 10
    while backend.microphone.frames_pushed < RATE and time.monotonic() < deadline:
        time.sleep(0.01)
    state.set_stop_flag()
    daemon.join(timeout=20)
    assert not daemon.is_alive(), "daemon did not exit after stop"


def test_silent_call_side_leaves_a_warning(monkeypatch, tmp_path):
    """A call side that never carried signal warns the user, yet the mp3 is still saved."""
    backend = FakeBackend(loopback=FakeSource(mode="live", level=0.0),
                          silence_hint="grant System Audio Recording")
    output = tmp_path / "audio" / "rec.mp3"
    _run_daemon_for_a_second(backend, monkeypatch, output)

    assert "grant System Audio Recording" in (state.pop_warning() or "")
    assert "WARNING" in state.log_file().read_text(encoding="utf-8")
    assert output.exists() and output.stat().st_size > 0


def test_audible_call_side_leaves_no_warning(monkeypatch, tmp_path):
    """Signal on the call side means the permission is fine, so nothing is reported."""
    backend = FakeBackend(loopback=FakeSource(mode="live", level=0.1),
                          silence_hint="grant System Audio Recording")
    _run_daemon_for_a_second(backend, monkeypatch, tmp_path / "audio" / "rec.mp3")

    assert state.pop_warning() is None
    assert "WARNING" not in state.log_file().read_text(encoding="utf-8")


def test_empty_hint_never_warns(monkeypatch, tmp_path):
    """Backends with no permission to blame (Windows, Linux) stay quiet on a silent call."""
    backend = FakeBackend(loopback=FakeSource(mode="live", level=0.0), silence_hint="")
    _run_daemon_for_a_second(backend, monkeypatch, tmp_path / "audio" / "rec.mp3")

    assert state.pop_warning() is None


def test_unwritable_warning_does_not_fail_the_recording(monkeypatch, tmp_path):
    """A note that cannot be written is only a side effect; the saved recording still succeeds."""

    def refuse(_message: str) -> None:
        raise OSError("read-only state dir")

    backend = FakeBackend(loopback=FakeSource(mode="live", level=0.0),
                          silence_hint="grant System Audio Recording")
    monkeypatch.setattr(engine, "select_backend", lambda: backend)
    monkeypatch.setattr(state, "write_warning", refuse)
    stop = threading.Event()
    output = tmp_path / "audio" / "rec.mp3"
    outcome = {}

    def run() -> None:
        """Record until stopped, keeping the result or the exception for the assertions."""
        try:
            outcome["frames"] = engine.record(output, stop, lambda _msg: None, RATE, RATE // 10)
        except BaseException as exc:  # noqa: BLE001 - handed to the test to assert on
            outcome["exc"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while backend.microphone.frames_pushed < RATE and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(timeout=20)

    assert not worker.is_alive()
    assert "exc" not in outcome
    assert outcome["frames"] > 0
    assert output.exists() and output.stat().st_size > 0
