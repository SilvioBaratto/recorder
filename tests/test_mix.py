"""Tests for the pure stereo mix."""

import numpy as np
import pytest

from recorder.mix import fit_and_mix, mix_blocks


def _block(value: float, frames: int = 4, channels: int = 2) -> np.ndarray:
    """Return a constant-valued float32 block of the given shape."""
    return np.full((frames, channels), value, dtype=np.float32)


def test_sums_within_range():
    """Two sub-unity blocks add sample-by-sample."""
    out = mix_blocks(_block(0.2), _block(0.3))
    assert np.allclose(out, 0.5)
    assert out.dtype == np.float32


def test_clips_when_sum_exceeds_full_scale():
    """Two loud blocks clip at +/-1.0 instead of wrapping or overflowing."""
    assert np.allclose(mix_blocks(_block(0.8), _block(0.8)), 1.0)
    assert np.allclose(mix_blocks(_block(-0.8), _block(-0.8)), -1.0)


def test_truncates_to_shorter_block():
    """A ragged pair is aligned to the shorter length."""
    out = mix_blocks(_block(0.1, frames=6), _block(0.1, frames=4))
    assert len(out) == 4


def test_gain_scales_before_clipping():
    """Gain below 1.0 buys headroom against simultaneous speakers."""
    out = mix_blocks(_block(0.8), _block(0.8), gain=0.5)
    assert np.allclose(out, 0.8)


def test_channel_mismatch_raises():
    """Mixing blocks with different channel counts is a programming error."""
    with pytest.raises(ValueError):
        mix_blocks(_block(0.1, channels=2), _block(0.1, channels=1))


def test_fit_and_mix_follows_mic_length():
    """The mic block sets the output length even when loopback is longer."""
    out = fit_and_mix(_block(0.1, frames=10), _block(0.2, frames=4))
    assert len(out) == 4
    assert out.dtype == np.float32


def test_fit_and_mix_pads_short_loopback_with_silence():
    """A short loopback block contributes silence for the missing tail, not the mic twice."""
    loop = _block(0.3, frames=2)
    mic = _block(0.1, frames=5)
    out = fit_and_mix(loop, mic)
    assert np.allclose(out[:2], 0.4)
    assert np.allclose(out[2:], 0.1)


def test_fit_and_mix_handles_empty_loopback():
    """An idle loopback yields the mic alone — the recording never stalls."""
    out = fit_and_mix(np.zeros((0, 2), dtype=np.float32), _block(0.2, frames=4))
    assert len(out) == 4
    assert np.allclose(out, 0.2)


def test_fit_and_mix_applies_mic_gain():
    """Mic gain lifts the voice; here a silent loopback makes the effect exact."""
    out = fit_and_mix(np.zeros((0, 2), dtype=np.float32), _block(0.2, frames=4), mic_gain=2.0)
    assert np.allclose(out, 0.4)
