"""Tests for the macOS backend, run against in-memory catap and sounddevice fakes on any OS."""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import numpy as np
import pytest

from recorder.backends.base import SourceInfo
from recorder.backends.macos import MacBackend, check_preconditions, convert_buffer

WAIT = 2.0
SUPPORTED_MAC = (15, 0)
SUPPORTED_PYTHON = (3, 12)
TAP_NAME = "System audio (Core Audio tap)"
MIC = {"name": "MacBook Pro Microphone", "default_samplerate": 48000.0, "max_input_channels": 1}

_IMPORT_MACOS = """
import sys
import recorder.backends.macos
recorder.backends.macos.MacBackend()
loaded = {"pyaudiowpatch", "sounddevice", "catap"} & set(sys.modules)
assert not loaded, sorted(loaded)
"""


def _format(sample_type: str = "float", bits: int = 32, channels: int = 2,
            rate: float = 48000.0) -> SimpleNamespace:
    return SimpleNamespace(sample_rate=rate, num_channels=channels, bits_per_sample=bits,
                           sample_type=sample_type)


def _buffer(*values: float, fmt: Optional[SimpleNamespace] = None) -> SimpleNamespace:
    fmt = fmt or _format()
    return SimpleNamespace(data=np.array(values, dtype="<f4").tobytes(),
                           frame_count=len(values) // fmt.num_channels, format=fmt)


class FakeSession:
    """Stands in for catap's ``RecordingSession``; tests push buffers through its callback.

    Attributes:
        on_buffer: The callback the backend registered.
        started: Whether ``start`` was called.
        capture_failed: Set by a test to simulate a Core Audio failure mid-recording.
        needs_cleanup: Whether a failed ``stop`` left resources for ``close`` to retry.
        stop_threads: The thread of every ``stop`` call, in order.
        close_calls: How many times ``close`` was called.
        drain: Buffers ``stop`` delivers while shutting down, like catap's queued audio.
    """

    def __init__(self, on_buffer: Any, stream_format: Optional[SimpleNamespace],
                 start_error: Optional[BaseException] = None,
                 stop_error: Optional[BaseException] = None,
                 cleanup_pending: bool = False) -> None:
        self.on_buffer = on_buffer
        self.started = False
        self.capture_failed = False
        self.needs_cleanup = False
        self.stop_threads: List[threading.Thread] = []
        self.close_calls = 0
        self.drain: List[SimpleNamespace] = []
        self._format = stream_format
        self._start_error = start_error
        self._stop_error = stop_error
        self._cleanup_pending = cleanup_pending
        self._callback_thread: Optional[threading.Thread] = None

    @property
    def stream_format(self) -> Optional[SimpleNamespace]:
        """Return the native format, which catap only knows once the tap runs."""
        return self._format if self.started else None

    def start(self) -> None:
        """Start the tap, or fail like a tap Core Audio refuses to create."""
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    def push(self, buffer: SimpleNamespace) -> None:
        """Deliver one buffer the way catap's worker thread does."""
        previous = self._callback_thread
        self._callback_thread = threading.current_thread()
        try:
            self.on_buffer(buffer)
        finally:
            self._callback_thread = previous

    def stop(self) -> None:
        """Stop the tap, refusing a call from inside ``on_buffer`` exactly like catap does."""
        if threading.current_thread() is self._callback_thread:
            raise RuntimeError("Cannot call stop() from an on_buffer callback")
        self.stop_threads.append(threading.current_thread())
        for buffer in self.drain:
            self.push(buffer)
        if self._stop_error is not None:
            self.needs_cleanup = self._cleanup_pending
            raise self._stop_error

    def close(self) -> None:
        """Release whatever a failed stop left behind."""
        self.close_calls += 1
        self.needs_cleanup = False


class FakeCatap:
    """Module stand-in exposing ``record_system_audio``; keeps every session it creates."""

    def __init__(self, stream_format: Optional[SimpleNamespace] = None, **session_options: Any) -> None:
        self.stream_format = stream_format if stream_format is not None else _format()
        self.session_options = session_options
        self.sessions: List[FakeSession] = []
        self.calls: List[Dict[str, Any]] = []

    def record_system_audio(self, output_path: Any = None, **kwargs: Any) -> FakeSession:
        """Create a streaming session; a file output would bypass the callback."""
        assert output_path is None
        self.calls.append(kwargs)
        session = FakeSession(kwargs["on_buffer"], self.stream_format, **self.session_options)
        self.sessions.append(session)
        return session


class FakeInputStream:
    """``sounddevice.InputStream`` stand-in with 480 frames ready once, then none."""

    def __init__(self, events: List[str], **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.events = events
        self._ready = [480]

    @property
    def read_available(self) -> int:
        """Return the frames that can be read without blocking."""
        return self._ready.pop() if self._ready else 0

    def start(self) -> None:
        """Record the start."""
        self.events.append("start")

    def read(self, frames: int):
        """Return ``frames`` frames and the overflow flag, like sounddevice."""
        self.events.append("read")
        return np.full((frames, self.kwargs["channels"]), 0.25, dtype=np.float32), False

    def stop(self) -> None:
        """Record the stop."""
        self.events.append("stop")

    def close(self) -> None:
        """Record the close."""
        self.events.append("close")


class FakeSoundDevice:
    """Module stand-in exposing ``query_devices`` and ``InputStream``."""

    def __init__(self, device: Optional[Dict[str, Any]] = None,
                 query_error: Optional[BaseException] = None) -> None:
        self.device = device if device is not None else MIC
        self.query_error = query_error
        self.query_kinds: List[Optional[str]] = []
        self.events: List[str] = []
        self.streams: List[FakeInputStream] = []

    def query_devices(self, device: Any = None, kind: Optional[str] = None) -> Dict[str, Any]:
        """Return the default device of ``kind``, or fail like a Mac with no input."""
        self.query_kinds.append(kind)
        if self.query_error is not None:
            raise self.query_error
        return self.device

    def InputStream(self, **kwargs: Any) -> FakeInputStream:  # noqa: N802 - mirrors sounddevice
        """Open a fake stream that logs into the shared event list."""
        stream = FakeInputStream(self.events, **kwargs)
        self.streams.append(stream)
        return stream


class FakePortAudioError(Exception):
    """Mimics ``sounddevice.PortAudioError``, which is not a ``RuntimeError``."""


class Probe:
    """Records what a source delivers so tests can wait on events instead of sleeping."""

    def __init__(self) -> None:
        self.blocks: List[np.ndarray] = []
        self.ends: List[Optional[BaseException]] = []
        self.got_block = threading.Event()
        self.ended = threading.Event()

    def on_block(self, block: np.ndarray) -> None:
        """Store the block and wake the test."""
        self.blocks.append(block)
        self.got_block.set()

    def on_end(self, error: Optional[BaseException]) -> None:
        """Store the end reason and wake the test."""
        self.ends.append(error)
        self.ended.set()


@pytest.fixture(autouse=True)
def no_leaked_threads() -> Iterator[None]:
    """Fail a test that leaves a watcher or reader thread behind."""
    yield
    leaked = [t for t in threading.enumerate()
              if t.name.startswith(("core-audio-tap", "polled-source-"))]
    for thread in leaked:
        thread.join(WAIT)
    assert not [t for t in leaked if t.is_alive()]


def _backend(catap: Optional[FakeCatap] = None, sd: Optional[FakeSoundDevice] = None,
             mac=SUPPORTED_MAC, python=SUPPORTED_PYTHON) -> MacBackend:
    return MacBackend(catap_module=catap or FakeCatap(), sounddevice_module=sd or FakeSoundDevice(),
                      mac_version=mac, python_version=python)


def _watchers_alive() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "core-audio-tap-watch" and t.is_alive()]


def test_check_preconditions_old_macos():
    """Process taps only exist from macOS 14.2, so older systems are told to upgrade."""
    with pytest.raises(RuntimeError, match=r"14\.2"):
        check_preconditions((13, 6), (3, 12), True)


def test_check_preconditions_old_python():
    """catap needs Python 3.11, which is also why it is missing on older interpreters."""
    with pytest.raises(RuntimeError, match=r"3\.11"):
        check_preconditions((15, 0), (3, 9), False)


def test_check_preconditions_missing_catap():
    """A supported system without catap is told how to install it; otherwise all is well."""
    with pytest.raises(RuntimeError, match="pip install catap"):
        check_preconditions((15, 0), (3, 12), False)
    assert check_preconditions((15, 0), (3, 12), True) is None


def test_convert_buffer_formats():
    """Every format catap can deliver becomes float32 ``(n, channels)`` in [-1, 1]."""
    stereo = np.array([[0.1, -0.2], [0.3, -0.4]], dtype=np.float32)
    block = convert_buffer(stereo.astype("<f4").tobytes(), "float", 32, 2)
    np.testing.assert_array_equal(block, stereo)
    assert block.dtype == np.float32
    assert block.flags.writeable

    half16 = convert_buffer(np.array([16384], dtype="<i2").tobytes(), "signed_integer", 16, 1)
    assert half16.dtype == np.float32
    assert half16.shape == (1, 1)
    assert half16[0, 0] == 0.5

    half32 = convert_buffer(np.array([1 << 30], dtype="<i4").tobytes(), "signed_integer", 32, 1)
    assert half32.dtype == np.float32
    assert half32[0, 0] == 0.5

    mono = convert_buffer(np.zeros(3, dtype="<f4").tobytes(), "float", 32, 1)
    assert mono.shape == (3, 1)

    with pytest.raises(ValueError, match="24"):
        convert_buffer(b"\x00" * 6, "signed_integer", 24, 1)


@pytest.mark.parametrize("sample_type, bits", [("float", 64), ("unsigned_integer", 16)])
def test_convert_buffer_rejects_other_formats(sample_type, bits):
    """Guessing a layout would deliver noise, so anything unknown fails naming the format."""
    with pytest.raises(ValueError, match=str(bits)):
        convert_buffer(b"\x00" * 16, sample_type, bits, 1)


def test_convert_buffer_drops_a_trailing_partial_frame():
    """A buffer that ends mid-frame keeps its whole frames instead of failing."""
    data = np.array([0.5, -0.5, 0.25], dtype="<f4").tobytes()
    block = convert_buffer(data, "float", 32, 2)
    np.testing.assert_array_equal(block, np.array([[0.5, -0.5]], dtype=np.float32))


def test_loopback_opens_a_running_streaming_tap_with_its_native_format():
    """The tap runs from open time, unmuted and without a file, so its format fills ``info``."""
    catap = FakeCatap(stream_format=_format(channels=2, rate=44100.4))
    source = _backend(catap=catap).open_loopback()
    try:
        assert source.info == SourceInfo(name=TAP_NAME, sample_rate=44100, channels=2)
        assert catap.sessions[0].started
        call = catap.calls[0]
        assert callable(call["on_buffer"])
        assert call.get("mute", False) is False
    finally:
        source.close()


def test_loopback_streams_converted_blocks_and_stops_once():
    """Blocks flow only after start; close stops the tap once and ends the stream once."""
    catap = FakeCatap()
    source = _backend(catap=catap).open_loopback()
    session = catap.sessions[0]
    probe = Probe()

    session.push(_buffer(0.9, 0.9))
    source.start(probe.on_block, probe.on_end)
    session.push(_buffer(0.5, -0.5, 0.25, -0.25))
    session.drain.append(_buffer(0.7, 0.7))
    source.close()
    source.close()

    assert len(probe.blocks) == 1
    np.testing.assert_array_equal(probe.blocks[0],
                                  np.array([[0.5, -0.5], [0.25, -0.25]], dtype=np.float32))
    assert probe.blocks[0].dtype == np.float32
    assert session.stop_threads == [threading.current_thread()]
    assert probe.ends == [None]
    assert not _watchers_alive()


def test_loopback_capture_failure_reaches_on_end():
    """A failure catap detects mid-recording ends the stream with an error, exactly once."""
    catap = FakeCatap()
    source = _backend(catap=catap).open_loopback()
    session = catap.sessions[0]
    probe = Probe()
    source.start(probe.on_block, probe.on_end)

    session.capture_failed = True
    assert probe.ended.wait(WAIT)
    session.push(_buffer(0.5, 0.5))
    source.close()

    assert len(probe.ends) == 1
    assert isinstance(probe.ends[0], RuntimeError)
    assert probe.blocks == []
    assert len(session.stop_threads) == 1
    # catap's own reason never reaches the log, so the message has to name the likely causes.
    message = str(probe.ends[0])
    assert message.startswith("Core Audio reported a system-audio capture failure while recording")
    assert "route" in message and "format" in message
    assert "tap was destroyed" in message and "256" in message
    assert "recorder devices" in message


def test_loopback_on_block_failure_ends_without_stopping_inside_the_callback():
    """A consumer error is reported once; the tap is only stopped later, by close."""
    catap = FakeCatap()
    source = _backend(catap=catap).open_loopback()
    session = catap.sessions[0]
    probe = Probe()
    boom = ValueError("consumer broke")

    def on_block(block: np.ndarray) -> None:
        """Fail like a consumer that cannot keep the block."""
        raise boom

    source.start(on_block, probe.on_end)
    session.push(_buffer(0.5, 0.5))
    session.push(_buffer(0.5, 0.5))
    assert probe.ends == [boom]
    assert session.stop_threads == []
    source.close()
    assert probe.ends == [boom]
    assert session.stop_threads == [threading.current_thread()]


def test_loopback_close_from_on_block_stops_outside_the_callback():
    """Closing from the callback must not call stop() on catap's worker, which would refuse it."""
    catap = FakeCatap()
    source = _backend(catap=catap).open_loopback()
    session = catap.sessions[0]
    probe = Probe()

    def on_block(block: np.ndarray) -> None:
        """Stop the source from inside catap's callback."""
        source.close()

    source.start(on_block, probe.on_end)
    session.push(_buffer(0.5, 0.5))
    assert probe.ended.wait(WAIT)
    assert probe.ends == [None]
    assert len(session.stop_threads) == 1
    assert session.stop_threads[0] is not threading.current_thread()


def test_loopback_close_before_start_stops_the_tap_and_never_ends():
    """A tap opened and discarded (the microphone failed to open) is still torn down."""
    catap = FakeCatap()
    source = _backend(catap=catap).open_loopback()
    source.close()
    source.close()
    assert len(catap.sessions[0].stop_threads) == 1
    probe = Probe()
    with pytest.raises(RuntimeError, match="closed"):
        source.start(probe.on_block, probe.on_end)
    assert not probe.ended.is_set()


def test_loopback_failed_watcher_start_still_lets_close_stop_the_tap(monkeypatch):
    """If the OS refuses the watcher thread, close() stops the tap and reports no end."""
    catap = FakeCatap()
    source = _backend(catap=catap).open_loopback()
    session = catap.sessions[0]
    probe = Probe()

    def _refuse(self) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)
    with pytest.raises(RuntimeError, match="new thread"):
        source.start(probe.on_block, probe.on_end)
    monkeypatch.undo()
    session.push(_buffer(0.5, 0.5))
    source.close()

    assert len(session.stop_threads) == 1
    assert probe.blocks == []
    assert probe.ends == []


def test_loopback_failed_teardown_is_retried_and_reported():
    """catap keeps a tap whose stop failed until close() succeeds; the error reaches on_end."""
    failure = OSError("teardown failed")
    catap = FakeCatap(stop_error=failure, cleanup_pending=True)
    source = _backend(catap=catap).open_loopback()
    session = catap.sessions[0]
    probe = Probe()
    source.start(probe.on_block, probe.on_end)
    source.close()
    assert session.close_calls == 1
    assert not session.needs_cleanup
    assert probe.ends == [failure]


@pytest.mark.parametrize("stream_format, reason", [
    (None, "format"),
    (_format(sample_type="signed_integer", bits=24), "24"),
])
def test_loopback_without_a_usable_format_releases_the_tap(stream_format, reason):
    """A tap that started but cannot be read is stopped before open fails."""
    catap = FakeCatap()
    catap.stream_format = stream_format
    backend = _backend(catap=catap)
    with pytest.raises(RuntimeError, match=reason):
        backend.open_loopback()
    assert len(catap.sessions[0].stop_threads) == 1


def test_loopback_start_failure_is_a_runtime_error():
    """Core Audio refusing the tap surfaces as the protocol's RuntimeError, with cleanup."""
    catap = FakeCatap(start_error=OSError("tap refused"))
    with pytest.raises(RuntimeError, match="tap refused"):
        _backend(catap=catap).open_loopback()
    assert catap.sessions[0].close_calls == 1


@pytest.mark.parametrize("error", [
    ValueError("unsupported tap format"),
    RuntimeError("native Core Audio error"),
])
def test_loopback_start_failure_of_any_kind_is_wrapped_with_cleanup(error):
    """catap's format and native errors are not OSErrors; they still get the message and cleanup."""
    catap = FakeCatap(start_error=error)
    with pytest.raises(RuntimeError) as excinfo:
        _backend(catap=catap).open_loopback()
    assert str(excinfo.value) == "could not start the Core Audio system-audio tap: {}".format(error)
    assert excinfo.value.__cause__ is error
    assert catap.sessions[0].close_calls == 1


def test_loopback_preconditions_run_before_any_tap():
    """An unsupported macOS fails with the fix before catap is asked for anything."""
    catap = FakeCatap()
    with pytest.raises(RuntimeError, match=r"14\.2"):
        _backend(catap=catap, mac=(13, 6)).open_loopback()
    assert catap.sessions == []


@pytest.mark.parametrize("release", ["13.6.1", ""])
def test_default_macos_version_comes_from_the_system(monkeypatch, release):
    """Without an injected version the running system's release is checked."""
    monkeypatch.setattr(platform, "mac_ver", lambda: (release, ("", "", ""), "arm64"))
    backend = MacBackend(catap_module=FakeCatap(), sounddevice_module=FakeSoundDevice())
    with pytest.raises(RuntimeError, match=r"14\.2") as info:
        backend.open_loopback()
    assert release in str(info.value)


def test_microphone_polls_and_closes_cleanly():
    """The default input opens at its native rate, capped to stereo, and is polled, not blocked on."""
    sd = FakeSoundDevice(device={"name": "Studio Interface", "default_samplerate": 44100.0,
                                 "max_input_channels": 4})
    source = _backend(sd=sd).open_microphone()
    probe = Probe()
    assert source.info == SourceInfo(name="Studio Interface", sample_rate=44100, channels=2)
    assert sd.query_kinds == ["input"]
    assert sd.streams[0].kwargs == {"samplerate": 44100, "channels": 2, "dtype": "float32"}

    source.start(probe.on_block, probe.on_end)
    try:
        assert probe.got_block.wait(WAIT)
    finally:
        source.close()
    source.close()

    assert probe.blocks[0].shape == (480, 2)
    assert sd.events == ["start", "read", "stop", "close"]
    assert probe.ends == [None]


@pytest.mark.parametrize("error", [ValueError("No input device matching"),
                                   FakePortAudioError("Error querying device -1")])
def test_no_input_device_message(error):
    """A Mac with no usable input is pointed at the Sound settings and the Microphone permission."""
    sd = FakeSoundDevice(query_error=error)
    with pytest.raises(RuntimeError) as info:
        _backend(sd=sd).open_microphone()
    message = str(info.value)
    assert "Sound" in message
    assert "Input" in message
    assert "Microphone" in message
    assert sd.streams == []


def test_describe_lists_the_tap_and_microphone_without_capturing():
    """Same three sections as the other backends, and no tap is started just to describe it."""
    catap = FakeCatap()
    text = _backend(catap=catap).describe()
    lines = text.splitlines()
    assert lines[0] == f"Call loopback source : {TAP_NAME}"
    assert lines[1] == "Microphone (your voice): MacBook Pro Microphone @ 48000 Hz"
    assert lines[2] == ""
    assert "System Audio Recording" in lines[3]
    assert "Microphone" in lines[3]
    assert len(lines) == 4
    assert catap.sessions == []


def test_describe_does_not_raise_on_failed_preconditions():
    """``recorder devices`` is the diagnostic, so it explains a broken setup instead of crashing."""
    sd = FakeSoundDevice(query_error=ValueError("No input device matching"))
    backend = MacBackend(sounddevice_module=sd, mac_version=(13, 6), python_version=(3, 12))
    lines = backend.describe().splitlines()
    assert lines[0].startswith("Call loopback source : UNAVAILABLE (")
    assert "14.2" in lines[0]
    assert lines[1].startswith("Microphone (your voice): UNAVAILABLE (")
    assert "Sound" in lines[1]
    assert "System Audio Recording" in lines[-1]


def test_silence_hint_names_the_permission_and_the_restart():
    """Zeroed tap buffers mean a missing permission, which only applies after a terminal restart."""
    hint = MacBackend.silence_hint
    assert "System Audio Recording" in hint
    assert "restart" in hint


def test_backend_close_is_a_harmless_no_op():
    """Every source owns its own tap or stream, so the backend has nothing to release."""
    catap = FakeCatap()
    backend = _backend(catap=catap)
    backend.close()
    backend.close()
    assert catap.sessions == []


def test_macos_backend_imports_no_audio_library():
    """Importing and constructing the backend must work on hosts without catap or sounddevice."""
    result = subprocess.run([sys.executable, "-c", _IMPORT_MACOS], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
