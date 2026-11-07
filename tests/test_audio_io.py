"""Tests for the per-device background reader."""

from __future__ import annotations

import threading

from fake_backend import FakeSource
from fake_pyaudio import LOOPBACK, RATE, FakePyAudio, FakePyAudioModule, FakeWindowsBackend
from recorder.audio_io import DeviceStream
from recorder.backends.base import OnBlock, OnEnd, SourceInfo

WAIT = 2.0


def test_silent_device_releases_its_stream_on_stop():
    """A loopback with nothing playing must not trap its reader inside read() past stop.

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
