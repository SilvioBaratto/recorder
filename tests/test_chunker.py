"""Tests for cutting a recording into upload-sized chunks at quiet moments."""

import lameenc
import numpy as np
import pytest

from recorder import chunker
from recorder.chunker import ChunkerError, find_quiet_cut, iter_chunks

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


@pytest.fixture
def miniaudio():
    """Skip the decoding tests when the optional transcribe extra is not installed."""
    return pytest.importorskip("miniaudio")


def make_mp3(path, parts):
    """Write a stereo 48 kHz 128 kbps mp3 of ``("tone" | "silence", seconds)`` parts."""
    rate = 48000
    pieces = []
    for kind, seconds in parts:
        count = int(seconds * rate)
        if kind == "tone":
            wave = (8000 * np.sin(2 * np.pi * 440 * np.arange(count) / rate)).astype(np.int16)
        else:
            wave = np.zeros(count, dtype=np.int16)
        pieces.append(np.repeat(wave, 2))
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(128)
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(2)
    encoder.set_quality(2)
    data = encoder.encode(np.concatenate(pieces).tobytes()) + encoder.flush()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


BURSTS = [("tone", 20), ("silence", 2)] * 5 + [("tone", 20)]
SILENCES = [(20, 22), (42, 44), (64, 66), (86, 88), (108, 110)]


def test_long_file_splits_at_silences(tmp_path, miniaudio):
    """Every cut lands in a pause, chunks tile the recording, and nothing is lost."""
    path = make_mp3(tmp_path / "meeting.mp3", BURSTS)
    chunks = list(iter_chunks(path, target_s=40, search_s=15))
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert any(start - 0.2 <= chunk.start_s <= end + 0.2 for start, end in SILENCES)
    for before, after in zip(chunks, chunks[1:]):
        assert after.start_s == pytest.approx(before.start_s + before.duration_s)
    assert chunks[0].start_s == 0
    assert sum(c.duration_s for c in chunks) == pytest.approx(130, abs=1)


def test_each_chunk_is_small_mono_16k_mp3(tmp_path, miniaudio):
    """Chunks are what the upload expects: mono, 16 kHz, well under the size limit."""
    path = make_mp3(tmp_path / "meeting.mp3", BURSTS)
    for chunk in iter_chunks(path, target_s=40, search_s=15):
        assert len(chunk.mp3) < chunker.UPLOAD_LIMIT_BYTES
        decoded = miniaudio.mp3_read_s16(chunk.mp3)
        assert (decoded.nchannels, decoded.sample_rate) == (1, 16000)


def test_short_file_is_one_chunk(tmp_path, miniaudio):
    """A recording shorter than the target goes out whole."""
    path = make_mp3(tmp_path / "short.mp3", [("tone", 10)])
    chunks = list(iter_chunks(path))
    assert len(chunks) == 1
    assert chunks[0].start_s == 0
    assert chunks[0].duration_s == pytest.approx(10, abs=0.1)


def test_file_under_half_a_second_raises(tmp_path, miniaudio):
    """Nothing worth transcribing is reported as an error, not an empty result."""
    path = make_mp3(tmp_path / "blip.mp3", [("tone", 0.3)])
    with pytest.raises(ChunkerError, match="no audio"):
        list(iter_chunks(path))


def test_undecodable_file_raises(tmp_path, miniaudio):
    """A file that is not audio is reported as a ChunkerError, not a decoder traceback."""
    path = tmp_path / "x.mp3"
    path.write_text("this is not audio")
    with pytest.raises(ChunkerError):
        list(iter_chunks(path))


def test_path_with_space_and_accent(tmp_path, miniaudio):
    """Folder and file names with spaces and accents decode like any other."""
    path = make_mp3(tmp_path / "cartella è" / "riunione 1.mp3", [("tone", 5)])
    assert len(list(iter_chunks(path))) == 1


def test_oversize_chunk_raises(tmp_path, miniaudio, monkeypatch):
    """A chunk the API would reject is caught before any upload."""
    path = make_mp3(tmp_path / "meeting.mp3", [("tone", 10)])
    monkeypatch.setattr(chunker, "UPLOAD_LIMIT_BYTES", 1000)
    with pytest.raises(ChunkerError, match="upload limit"):
        list(iter_chunks(path))
