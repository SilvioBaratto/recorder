"""Platform-free ``Backend``/``Source`` stand-ins, so engine tests run on any OS without audio."""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from recorder.backends.base import OnBlock, OnEnd, SourceInfo

_JOIN_SECONDS = 2.0


class FakeSource:
    """A source that is either live (constant-level blocks at 10x real time) or silent.

    Attributes:
        info: Native format and name of the stream.
        mode: ``"live"`` pushes from its own thread; ``"silent"`` never pushes, like a
            loopback while nothing plays.
        level: Sample value every pushed frame carries.
        started: Whether ``start`` was called.
        closed: Whether ``close`` was called.
        frames_pushed: Native frames delivered so far, so tests can check rate conversion.
    """

    def __init__(self, name: str = "fake", rate: int = 48000, channels: int = 2,
                 mode: str = "live", level: float = 0.1) -> None:
        self.info = SourceInfo(name=name, sample_rate=rate, channels=channels)
        self.mode = mode
        self.level = level
        self.started = False
        self.closed = False
        self.frames_pushed = 0
        self._on_end: Optional[OnEnd] = None
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Begin delivering blocks; a silent source only remembers ``on_end``."""
        self.started = True
        self._on_end = on_end
        if self.mode == "live":
            self._thread = threading.Thread(target=self._run, args=(on_block,), daemon=True)
            self._thread.start()

    def _run(self, on_block: OnBlock) -> None:
        frames = self.info.sample_rate // 10
        # 0.1 s of audio every 0.01 s: 10x real time keeps tests fast.
        while not self._halt.wait(0.01):
            on_block(np.full((frames, self.info.channels), self.level, dtype=np.float32))
            self.frames_pushed += frames

    def close(self) -> None:
        """Stop pushing, then report a clean end once if the source was started."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(_JOIN_SECONDS)
        if self.started and self._on_end is not None:
            self._on_end(None)


class FakeBackend:
    """Hands out fixed fake sources and records whether it was closed.

    Attributes:
        loopback: Source returned by ``open_loopback``.
        microphone: Source returned by ``open_microphone``.
        silence_hint: Advice a silent capture would show.
        closed: Whether ``close`` was called.
    """

    def __init__(self, loopback: Optional[FakeSource] = None,
                 microphone: Optional[FakeSource] = None, silence_hint: str = "",
                 fail_microphone: bool = False) -> None:
        self.loopback = loopback if loopback is not None else FakeSource(name="fake loopback")
        self.microphone = (microphone if microphone is not None
                           else FakeSource(name="fake microphone"))
        self.silence_hint = silence_hint
        self.fail_microphone = fail_microphone
        self.closed = False

    def open_loopback(self) -> FakeSource:
        """Return the loopback source."""
        return self.loopback

    def open_microphone(self) -> FakeSource:
        """Return the microphone source, or fail like a machine with no input device.

        Raises:
            RuntimeError: If built with ``fail_microphone=True``.
        """
        if self.fail_microphone:
            raise RuntimeError("fake microphone failed to open")
        return self.microphone

    def describe(self) -> str:
        """Name the two sources."""
        return f"loopback: {self.loopback.info.name}\nmicrophone: {self.microphone.info.name}"

    def close(self) -> None:
        """Record the teardown."""
        self.closed = True
