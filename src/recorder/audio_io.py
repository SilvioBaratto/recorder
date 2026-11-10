"""Per-device buffering: turn a backend source's native blocks into stereo at a common rate.

Imports no audio library: the platform backend owns the device and pushes blocks in, so this
module and the mixer above it behave the same on every operating system.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np

from .backends.base import Source
from .resample import LinearResampler

Log = Callable[[str], None]


def _to_stereo(block: np.ndarray, channels: int) -> np.ndarray:
    """Coerce an ``(n, channels)`` block to ``(n, 2)`` by duplicating mono or dropping extras."""
    if channels == 1:
        return np.repeat(block, 2, axis=1)
    if channels == 2:
        return block
    return np.ascontiguousarray(block[:, :2])


class DeviceStream:
    """Buffer that yields stereo float32 frames at a common target rate from one source.

    The source pushes native blocks from its own thread; each is converted to stereo,
    resampled to ``target_rate`` and appended to an internal buffer. :meth:`pull` hands out
    fixed-size chunks to the mixer, blocking until enough frames exist or the stream ends.
    """

    def __init__(self, source: Source, target_rate: int, stop_event: threading.Event,
                 log: Log, label: str) -> None:
        """Wrap an opened, unstarted source.

        Args:
            source: Device to read; this stream starts and closes it.
            target_rate: Rate in Hz every pulled frame is resampled to.
            stop_event: Shared recording stop signal; set here when the source fails.
            log: Sink for diagnostic lines.
            label: Name of the stream in log lines, such as ``"mic"``.
        """
        self.label = label
        self._source = source
        self._stop = stop_event
        self._log = log
        self._channels = source.info.channels
        self._resampler = LinearResampler(source.info.sample_rate, target_rate, channels=2)
        self._buf = np.zeros((0, 2), dtype=np.float32)
        self._max_buffer = target_rate * 10  # drop oldest beyond ~10 s to bound latency/memory
        self._cond = threading.Condition()
        self._finished = False
        self._heard_signal = False

    @property
    def heard_signal(self) -> bool:
        """Whether any delivered block carried a non-zero sample.

        A device that is open but never carries signal is how macOS reports a missing System
        Audio Recording permission, so this is the only evidence of it.
        """
        return self._heard_signal

    def start(self) -> None:
        """Begin delivery from the source's own thread."""
        self._source.start(self._on_block, self._on_end)

    def _on_block(self, native: np.ndarray) -> None:
        # A source thread can still deliver after stop while it winds down; those frames must
        # not reach the mix. The wake-up matters: a source no longer exits on stop by itself, so
        # without it a mixer parked in pull() would sit out its whole 0.5 s wait.
        if self._stop.is_set():
            with self._cond:
                self._cond.notify_all()
            return
        if not self._heard_signal and native.any():
            self._heard_signal = True
        frames = self._resampler.process(_to_stereo(native, self._channels))
        if len(frames):
            with self._cond:
                self._buf = np.concatenate([self._buf, frames])
                if len(self._buf) > self._max_buffer:
                    self._buf = self._buf[-self._max_buffer:]
                self._cond.notify_all()

    def _on_end(self, error: Optional[BaseException]) -> None:
        try:
            if error is not None:
                self._log(f"{self.label} capture error: {error!r}")
        finally:
            # Runs even when logging raises: a broken log sink must not leave the mixer waiting
            # on a stream that already ended.
            if error is not None:
                # One failed device ends the whole recording, so what was captured gets saved.
                self._stop.set()
            with self._cond:
                self._finished = True
                self._cond.notify_all()

    def pull(self, frames: int) -> np.ndarray:
        """Return up to ``frames`` buffered frames, blocking until available or finished.

        Returns fewer than ``frames`` (possibly zero) once the stream has finished, the stop
        signal is set, or its buffer is drained — which is how the mixer learns to stop. The
        stop check also unblocks the mixer when a device stops delivering frames.
        """
        with self._cond:
            while len(self._buf) < frames and not self._finished and not self._stop.is_set():
                self._cond.wait(timeout=0.5)
            take = min(frames, len(self._buf))
            out = self._buf[:take]
            self._buf = self._buf[take:]
            return out

    def pull_nowait(self, frames: int) -> np.ndarray:
        """Return up to ``frames`` buffered frames immediately, without blocking.

        Used for the loopback in the mic-driven loop: take whatever call audio is ready now
        (possibly none) to mix against the current mic block.
        """
        with self._cond:
            take = min(frames, len(self._buf))
            out = self._buf[:take]
            self._buf = self._buf[take:]
            return out

    def join(self) -> None:
        """Close the source, waiting for its thread to stop delivering and release the device."""
        self._source.close()
