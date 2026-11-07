"""A small streaming linear resampler, so a mic and loopback at different rates can be mixed.

Headset mics often run at a different sample rate than the speaker (16 kHz in a call vs 48 kHz
playback), so each stream is resampled to a common rate before mixing. Linear interpolation is
more than enough for speech, and the stateful design keeps phase continuous across blocks.
"""

from __future__ import annotations

import numpy as np


class LinearResampler:
    """Resample a stream of stereo float32 blocks from ``src_rate`` to ``dst_rate``.

    Feed consecutive blocks to :meth:`process`; it returns the resampled frames available so
    far and carries the remainder (plus sub-sample phase) into the next call, so there are no
    discontinuities at block boundaries. When the rates are equal it is a pass-through.
    """

    def __init__(self, src_rate: int, dst_rate: int, channels: int = 2) -> None:
        self._step = src_rate / dst_rate
        self._channels = channels
        self._buf = np.zeros((0, channels), dtype=np.float32)
        self._phase = 0.0

    @property
    def passthrough(self) -> bool:
        """Whether source and destination rates match, making resampling a no-op."""
        return self._step == 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        """Resample one input block, returning the output frames ready so far.

        Args:
            block: Input frames shaped ``(n, channels)`` at the source rate.

        Returns:
            Output frames shaped ``(m, channels)`` at the destination rate; ``m`` may be 0.
        """
        if self.passthrough:
            return block
        self._buf = np.concatenate([self._buf, block]) if len(self._buf) else block
        available = len(self._buf)
        if available < 2:
            return np.zeros((0, self._channels), dtype=np.float32)

        positions = []
        pos = self._phase
        while pos <= available - 1 - 1e-9:
            positions.append(pos)
            pos += self._step

        if not positions:
            return np.zeros((0, self._channels), dtype=np.float32)

        positions = np.asarray(positions)
        left = np.floor(positions).astype(np.int64)
        frac = (positions - left).astype(np.float32)[:, None]
        out = self._buf[left] * (1.0 - frac) + self._buf[left + 1] * frac

        consumed = int(np.floor(pos))
        self._buf = self._buf[consumed:]
        self._phase = pos - consumed
        return out.astype(np.float32, copy=False)
