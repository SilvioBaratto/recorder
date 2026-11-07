"""Tests for the streaming linear resampler."""

import numpy as np

from recorder.resample import LinearResampler


def _stereo_ramp(n: int) -> np.ndarray:
    """Return an ``(n, 2)`` float32 ramp from 0 to 1 on both channels."""
    x = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return np.stack([x, x], axis=1)


def test_passthrough_returns_input_unchanged():
    """Equal rates short-circuit to a pass-through."""
    resampler = LinearResampler(48000, 48000, 2)
    block = _stereo_ramp(100)
    assert resampler.passthrough
    assert np.array_equal(resampler.process(block), block)


def test_downsample_halves_total_length():
    """48 kHz -> 24 kHz over one second yields ~24000 frames across streamed blocks."""
    resampler = LinearResampler(48000, 24000, 2)
    total = sum(len(resampler.process(_stereo_ramp(4800))) for _ in range(10))
    assert abs(total - 24000) <= 5


def test_upsample_triples_total_length():
    """16 kHz -> 48 kHz over one second yields ~48000 frames across streamed blocks."""
    resampler = LinearResampler(16000, 48000, 2)
    total = sum(len(resampler.process(_stereo_ramp(1600)))  for _ in range(10))
    assert abs(total - 48000) <= 5


def test_output_is_stereo_float32():
    """Resampling preserves two channels and float32 dtype."""
    out = LinearResampler(48000, 44100, 2).process(_stereo_ramp(4800))
    assert out.dtype == np.float32
    assert out.shape[1] == 2


def test_constant_signal_stays_constant():
    """A constant input resamples to the same constant (linear interp of a flat line)."""
    resampler = LinearResampler(48000, 16000, 2)
    const = np.full((4800, 2), 0.5, dtype=np.float32)
    out = np.concatenate([resampler.process(const) for _ in range(5)])
    assert np.allclose(out, 0.5, atol=1e-3)
