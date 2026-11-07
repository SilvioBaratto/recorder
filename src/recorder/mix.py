"""Pure audio mixing — the one piece of real logic worth unit-testing in isolation."""

from __future__ import annotations

import numpy as np


def mix_blocks(loopback: np.ndarray, mic: np.ndarray, gain: float = 1.0) -> np.ndarray:
    """Sum the call and microphone blocks into one stereo block.

    Both inputs are float32 arrays shaped ``(frames, channels)`` in the range [-1, 1].
    The result is truncated to the shorter of the two (a block pair can differ by a frame
    at stream edges) and hard-clipped, since two full-scale signals can sum past full scale.

    Args:
        loopback: The call audio block (what you hear from other participants).
        mic: The microphone block (your own voice), same channel count as ``loopback``.
        gain: Overall scale applied before clipping. 1.0 preserves levels; lower it to
            trade loudness for headroom against simultaneous-speaker clipping.

    Returns:
        A float32 block shaped ``(min_frames, channels)`` in [-1, 1].

    Raises:
        ValueError: If the two blocks have different channel counts.
    """
    if loopback.ndim != 2 or mic.ndim != 2:
        raise ValueError("blocks must be 2-D (frames, channels)")
    if loopback.shape[1] != mic.shape[1]:
        raise ValueError(
            f"channel mismatch: loopback has {loopback.shape[1]}, mic has {mic.shape[1]}"
        )
    n = min(len(loopback), len(mic))
    out = (loopback[:n] + mic[:n]) * gain
    np.clip(out, -1.0, 1.0, out=out)
    return out.astype(np.float32, copy=False)


def fit_and_mix(loopback: np.ndarray, mic: np.ndarray, mic_gain: float = 1.0) -> np.ndarray:
    """Mix the mic block with whatever loopback is available right now.

    The mic is always live, so it sets the block length and drives the recording's timeline.
    The loopback block is truncated or zero-padded to match, meaning an idle or momentarily
    empty loopback never stalls capture or drops the user's voice — it just contributes
    silence for that slice.

    Args:
        loopback: The loopback frames available this tick, shaped ``(k, channels)``; ``k`` may
            be anything from 0 to more than ``len(mic)``.
        mic: The microphone block, shaped ``(n, channels)``, whose length is authoritative.
        mic_gain: Linear gain applied to the mic before mixing, to lift a quiet voice.

    Returns:
        A float32 block shaped ``(n, channels)`` in [-1, 1].
    """
    n = len(mic)
    if len(loopback) >= n:
        aligned = loopback[:n]
    else:
        aligned = np.zeros((n, mic.shape[1]), dtype=np.float32)
        if len(loopback):
            aligned[: len(loopback)] = loopback
    return mix_blocks(aligned, mic * np.float32(mic_gain))
