"""End-to-end daemon runs against fake devices: a recording must survive its own shutdown."""

from __future__ import annotations

import threading
import time

import pytest

from fake_backend import FakeBackend
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
