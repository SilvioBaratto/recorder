"""In-memory PyAudioWPatch stand-ins that reproduce how WASAPI devices behave around a call.

Two real behaviours are modelled because the recorder's shutdown depends on them: a loopback
endpoint with nothing playing reports no frames and its ``read()`` never returns, and tearing
PortAudio down while a thread is still inside ``read()`` kills the process.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable

import numpy as np

from recorder.backends.windows import WindowsBackend

RATE = 48000

LOOPBACK = {"index": 1, "name": "Fake Speakers [Loopback]", "defaultSampleRate": float(RATE),
            "maxInputChannels": 2}
MIC = {"index": 2, "name": "Fake Microphone", "defaultSampleRate": float(RATE),
       "maxInputChannels": 1}


class TeardownCrash(BaseException):
    """Stands in for the native crash: like a dying process, it skips ``except Exception``."""


class FakeStream:
    """A capture stream that is either live (frames at 10x real time) or silent (none, ever)."""

    def __init__(self, channels: int, rate: int, live: bool) -> None:
        self.channels = channels
        self.rate = rate
        self.live = live
        self.closed = False
        self.reading = False
        self.frames_delivered = 0
        self.touched = threading.Event()
        self._release = threading.Event()

    def get_read_available(self) -> int:
        """Return the frames readable without blocking: always 0 for a silent stream."""
        self.touched.set()
        return self.rate // 100 if self.live else 0

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        """Return ``frames`` of zeros; a silent stream blocks until :meth:`release`."""
        self.touched.set()
        self.reading = True
        try:
            if self.live:
                time.sleep(frames / self.rate / 10)
            else:
                self._release.wait()
            self.frames_delivered += frames
            return np.zeros(frames * self.channels, dtype=np.float32).tobytes()
        finally:
            self.reading = False

    def stop_stream(self) -> None:
        """Accept the stop; nothing to halt in memory."""

    def close(self) -> None:
        """Record that the owner closed the stream."""
        self.closed = True

    def release(self) -> None:
        """Unblock a silent stream's pending read so its thread can finish after the test."""
        self._release.set()


class FakePyAudio:
    """Opens a silent stream for :data:`LOOPBACK` and a live one for :data:`MIC`.

    Args:
        crash_on_terminate: Make ``terminate()`` crash even when no reader is blocked, to test
            that the recording is already safe before teardown starts.
    """

    def __init__(self, crash_on_terminate: bool = False) -> None:
        self.crash_on_terminate = crash_on_terminate
        self.streams: dict[int, FakeStream] = {}
        self.open_kwargs: dict[int, dict] = {}
        self.terminate_calls = 0

    def open(self, **kwargs) -> FakeStream:
        """Open a fake capture stream for the device at ``input_device_index``."""
        index = kwargs["input_device_index"]
        stream = FakeStream(kwargs["channels"], kwargs["rate"], live=index == MIC["index"])
        self.streams[index] = stream
        self.open_kwargs[index] = kwargs
        return stream

    def terminate(self) -> None:
        """Tear down, crashing like PortAudio if any stream is still inside ``read()``."""
        self.terminate_calls += 1
        if self.crash_on_terminate or any(s.reading for s in self.streams.values()):
            raise TeardownCrash("PortAudio torn down under a reader blocked in read()")

    def release_all(self) -> None:
        """Unblock every silent stream's pending read."""
        for stream in self.streams.values():
            stream.release()


class FakePyAudioModule:
    """Stands in for the ``pyaudiowpatch`` module, handing out one given :class:`FakePyAudio`."""

    paFloat32 = 1
    paWASAPI = 13

    def __init__(self, pa: FakePyAudio) -> None:
        self._pa = pa

    def PyAudio(self) -> FakePyAudio:  # noqa: N802 - mirrors the pyaudiowpatch API
        """Return the wrapped fake."""
        return self._pa


class FakeWindowsBackend(WindowsBackend):
    """The real Windows backend with WASAPI discovery pinned to :data:`LOOPBACK` and :data:`MIC`."""

    def find_loopback_device(self) -> dict:
        """Return the fake loopback endpoint."""
        return LOOPBACK

    def find_input_device(self) -> dict:
        """Return the fake microphone."""
        return MIC

    def _loopback_devices(self) -> Iterable[dict]:
        return [LOOPBACK]
