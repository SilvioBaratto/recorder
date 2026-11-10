"""Tests for the per-device background reader."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from fake_backend import FakeSource
from fake_pyaudio import LOOPBACK, RATE, FakePyAudio, FakePyAudioModule, FakeWindowsBackend
from recorder.audio_io import DeviceStream
from recorder.backends.base import OnBlock, OnEnd, SourceInfo

WAIT = 2.0


def test_silent_device_releases_its_stream_on_close():
    """A loopback with nothing playing must not trap its reader inside read() once closed.

    A reader still parked in PortAudio when the daemon tears audio down crashes the process.
    """
    pa = FakePyAudio()
    stop = threading.Event()
    source = FakeWindowsBackend(FakePyAudioModule(pa)).open_loopback()
    reader = DeviceStream(source, RATE, stop, lambda _msg: None, "loopback")
    device = pa.streams[LOOPBACK["index"]]
    reader.start()
    try:
        assert device.touched.wait(timeout=2)
        stop.set()
        reader.join()
        assert device.closed
        assert not device.reading
    finally:
        pa.release_all()


def test_16k_mono_source_comes_out_as_48k_stereo():
    """A 16 kHz mono headset reaches the mixer as 48 kHz stereo, three frames per input frame."""
    source = FakeSource(rate=16000, channels=1)
    stop = threading.Event()
    stream = DeviceStream(source, 48000, stop, lambda _msg: None, "mic")
    # Bounds the blocking pulls if no audio arrives, so a regression fails instead of hanging.
    guard = threading.Timer(WAIT, stop.set)
    guard.start()
    try:
        stream.start()
        blocks = [stream.pull(4800) for _ in range(5)]
    finally:
        guard.cancel()
        stream.join()
    rest = stream.pull_nowait(10 ** 9)

    assert abs(sum(len(b) for b in blocks) - 24000) <= 2
    assert all(b.ndim == 2 and b.shape[1] == 2 for b in blocks)
    produced = sum(len(b) for b in blocks) + len(rest)
    # The resampler holds the newest input frame back until the next one arrives to
    # interpolate against, so up to three output frames stay pending.
    assert 0 <= 3 * source.frames_pushed - produced <= 3


class _FailingSource:
    """Reports a device failure as soon as it is started."""

    info = SourceInfo(name="broken", sample_rate=48000, channels=2)

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """End delivery at once with a device error."""
        on_end(RuntimeError("boom"))

    def close(self) -> None:
        """Nothing to release."""


def test_source_error_stops_the_recording():
    """A failing device stops the whole recording and frees the mixer instead of stalling it."""
    stop = threading.Event()
    lines = []
    stream = DeviceStream(_FailingSource(), 48000, stop, lines.append, "loopback")
    stream.start()

    assert stop.is_set()
    assert any("capture error" in line for line in lines)
    puller = threading.Thread(target=stream.pull, args=(100,), daemon=True)
    puller.start()
    puller.join(WAIT)
    assert not puller.is_alive()


class _CapturingSource:
    """Hands the stream's callbacks back to the test so it can drive them by hand."""

    info = SourceInfo(name="manual", sample_rate=48000, channels=2)

    def __init__(self) -> None:
        self.on_block: OnBlock = lambda _block: None
        self.on_end: OnEnd = lambda _error: None

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Remember the callbacks instead of delivering anything."""
        self.on_block = on_block
        self.on_end = on_end

    def close(self) -> None:
        """Nothing to release."""


def test_blocks_after_stop_are_dropped():
    """Audio delivered after the stop signal is discarded, so a late block cannot extend the mix."""
    source = _CapturingSource()
    stop = threading.Event()
    stream = DeviceStream(source, 48000, stop, lambda _msg: None, "loopback")
    stream.start()
    stop.set()

    source.on_block(np.full((480, 2), 0.5, dtype=np.float32))

    assert len(stream.pull_nowait(10 ** 6)) == 0
    assert not stream.heard_signal


def test_on_end_still_stops_and_finishes_when_logging_fails():
    """A broken log sink must not leave the mixer blocked on a stream that already ended."""
    source = _CapturingSource()
    stop = threading.Event()

    def broken_log(_msg: str) -> None:
        raise OSError("disk full")

    stream = DeviceStream(source, 48000, stop, broken_log, "loopback")
    stream.start()

    with pytest.raises(OSError):
        source.on_end(RuntimeError("boom"))

    assert stop.is_set()
    # Cleared so that only the finished flag, not the stop signal, can release pull().
    stop.clear()
    result = []
    puller = threading.Thread(target=lambda: result.append(stream.pull(100)), daemon=True)
    puller.start()
    puller.join(WAIT)
    assert not puller.is_alive()


def test_heard_signal_tracks_non_zero_blocks():
    """Only a block with a non-zero sample counts as signal; zeroed buffers do not."""
    source = _CapturingSource()
    stream = DeviceStream(source, 48000, threading.Event(), lambda _msg: None, "loopback")
    stream.start()

    source.on_block(np.zeros((480, 2), dtype=np.float32))
    assert not stream.heard_signal

    source.on_block(np.full((480, 2), 0.1, dtype=np.float32))
    assert stream.heard_signal


def _time_pull_after(stream: DeviceStream, trigger) -> float:
    """Block a mixer-like thread in pull(), fire ``trigger``, and return how long pull took."""
    elapsed = []

    def mixer() -> None:
        began = time.monotonic()
        stream.pull(10 ** 9)
        elapsed.append(time.monotonic() - began)

    thread = threading.Thread(target=mixer, daemon=True)
    thread.start()
    time.sleep(0.1)
    fired = time.monotonic()
    trigger()
    thread.join(WAIT)
    assert elapsed, "pull() never returned after the trigger"
    return time.monotonic() - fired


def test_stop_wakes_a_mixer_waiting_for_frames_promptly():
    """The mixer parked in pull() must notice stop at once, not after the 0.5 s wait times out.

    A live device keeps delivering while it winds down, and that late block is the only thing
    that can wake the mixer once the reader thread no longer exits on stop by itself.
    """
    source = _CapturingSource()
    stop = threading.Event()
    stream = DeviceStream(source, 48000, stop, lambda _msg: None, "mic")
    stream.start()

    def stop_then_late_block() -> None:
        stop.set()
        source.on_block(np.zeros((480, 2), dtype=np.float32))

    woke_after = _time_pull_after(stream, stop_then_late_block)

    assert woke_after < 0.25


class _ManualFailure:
    """A source that stays quiet until the test tells it to fail."""

    info = SourceInfo(name="lost", sample_rate=48000, channels=2)

    def __init__(self) -> None:
        self.on_end: OnEnd = lambda _error: None

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Keep ``on_end`` so the test can end the stream by hand."""
        self.on_end = on_end

    def close(self) -> None:
        """Nothing to release."""


def test_a_failed_loopback_wakes_the_mixer_waiting_on_the_microphone():
    """One device failing sets stop; the mic stream's waiting mixer must wake without a timeout."""
    mic_source = _CapturingSource()
    lost = _ManualFailure()
    stop = threading.Event()
    mic = DeviceStream(mic_source, 48000, stop, lambda _msg: None, "mic")
    loopback = DeviceStream(lost, 48000, stop, lambda _msg: None, "loopback")
    mic.start()
    loopback.start()

    def lose_loopback_then_late_mic_block() -> None:
        lost.on_end(OSError("device lost"))
        mic_source.on_block(np.zeros((480, 2), dtype=np.float32))

    woke_after = _time_pull_after(mic, lose_loopback_then_late_mic_block)

    assert woke_after < 0.25
