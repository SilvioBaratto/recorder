"""In-memory PyAudioWPatch stand-ins that reproduce how WASAPI devices behave around a call.

Two real behaviours are modelled because the recorder's shutdown depends on them: a loopback
endpoint with nothing playing reports no frames and its ``read()`` never returns, and tearing
PortAudio down while a thread is still inside ``read()`` kills the process.
"""

from __future__ import annotations

import threading
import time

import numpy as np

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

    def open(self, *, channels: int, rate: int, input_device_index: int, **_kwargs) -> FakeStream:
        """Open a fake capture stream for the device at ``input_device_index``."""
        stream = FakeStream(channels, rate, live=input_device_index == MIC["index"])
        self.streams[input_device_index] = stream
        return stream

    def terminate(self) -> None:
        """Tear down, crashing like PortAudio if any stream is still inside ``read()``."""
        if self.crash_on_terminate or any(s.reading for s in self.streams.values()):
            raise TeardownCrash("PortAudio torn down under a reader blocked in read()")

    def release_all(self) -> None:
        """Unblock every silent stream's pending read."""
        for stream in self.streams.values():
            stream.release()
