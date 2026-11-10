"""Cut a recording into upload-sized mp3 chunks at quiet moments.

Knows nothing about OpenAI. The decoder (``miniaudio``) comes from the optional ``transcribe``
extra and is imported inside the function that needs it, so the pure cut-point logic here stays
importable and testable without that extra.
"""

from __future__ import annotations

import numpy as np

_FRAME_DIVISOR = 10
"""A quiet-moment search frame lasts ``1 / _FRAME_DIVISOR`` s: shorter than a word-final pause."""

_SMOOTHING_FRAMES = 3


def find_quiet_cut(samples: np.ndarray, target: int, search: int, rate: int) -> int:
    """Return the sample index of the quietest moment in ``samples[target:target + search]``.

    The region is measured in 100 ms frames. Each frame's loudness is averaged with its
    neighbours so one low-energy frame inside a word is not mistaken for a pause; only a
    stretch of quiet frames wins.

    Args:
        samples: Mono PCM samples (integer or float) of the audio buffered so far.
        target: Sample index where the chunk would ideally end.
        search: Number of samples after ``target`` that may hold the cut.
        rate: Sample rate of ``samples`` in Hz.

    Returns:
        Absolute index into ``samples`` at the centre of the quietest frame, so
        ``target <= result < target + search``.

    Raises:
        ValueError: If ``samples`` ends before ``target + search``.
    """
    if len(samples) < target + search:
        raise ValueError(
            f"need {target + search} samples to search for a cut, have {len(samples)}"
        )
    frame = max(rate // _FRAME_DIVISOR, 1)
    count = max(search // frame, 1)
    region = samples[target:target + count * frame].astype(np.float32)
    rms = np.sqrt(np.mean(region.reshape(count, frame) ** 2, axis=1))
    # Edge padding, not zero padding: zeros would make the first and last frames look quiet and
    # pull every cut to the edge of the search window.
    padded = np.pad(rms, _SMOOTHING_FRAMES // 2, mode="edge")
    smoothed = np.convolve(padded, np.ones(_SMOOTHING_FRAMES) / _SMOOTHING_FRAMES, mode="valid")
    return target + int(np.argmin(smoothed)) * frame + frame // 2
