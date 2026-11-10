"""Tests for cutting a recording into upload-sized chunks at quiet moments."""

import numpy as np
import pytest

from recorder.chunker import find_quiet_cut

RATE = 16000
TARGET = 10 * RATE
SEARCH = 5 * RATE


def _noise_with_gaps(seconds, gaps):
    """Return seeded loud noise lasting ``seconds`` with zeroed ``(start_s, end_s)`` gaps."""
    rng = np.random.default_rng(7)
    samples = rng.integers(-3000, 3000, size=int(seconds * RATE), dtype=np.int16)
    for start, end in gaps:
        samples[int(start * RATE):int(end * RATE)] = 0
    return samples


def test_cut_lands_in_silent_gap():
    """The cut falls inside the pause, not in the speech around it."""
    samples = _noise_with_gaps(20, [(12.0, 12.5)])
    assert 12.0 * RATE <= find_quiet_cut(samples, TARGET, SEARCH, RATE) < 12.5 * RATE


def test_gap_before_target_is_ignored():
    """A pause earlier than the target must not pull the cut short of the target."""
    samples = _noise_with_gaps(20, [(5.0, 5.5)])
    assert TARGET <= find_quiet_cut(samples, TARGET, SEARCH, RATE) < TARGET + SEARCH


def test_gap_after_search_is_ignored():
    """A pause beyond the search window must not push the chunk past its size budget."""
    samples = _noise_with_gaps(20, [(16.0, 16.5)])
    assert find_quiet_cut(samples, TARGET, SEARCH, RATE) < TARGET + SEARCH


def test_all_silence_returns_index_in_region():
    """With nothing to choose from, any point in the search region is acceptable."""
    samples = np.zeros(20 * RATE, dtype=np.int16)
    assert TARGET <= find_quiet_cut(samples, TARGET, SEARCH, RATE) < TARGET + SEARCH


def test_short_buffer_raises():
    """A buffer that does not reach the end of the search region cannot be cut."""
    samples = _noise_with_gaps(14, [])
    with pytest.raises(ValueError):
        find_quiet_cut(samples, TARGET, SEARCH, RATE)
