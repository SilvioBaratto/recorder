"""Cut a recording into upload-sized mp3 chunks at quiet moments.

Knows nothing about OpenAI. The decoder (``miniaudio``) comes from the optional ``transcribe``
extra and is imported inside the function that needs it, so the pure cut-point logic here stays
importable and testable without that extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import lameenc
import numpy as np

from .config import (
    CHUNK_SEARCH_SECONDS,
    CHUNK_TARGET_SECONDS,
    STT_BITRATE,
    STT_SAMPLERATE,
    UPLOAD_LIMIT_BYTES,
)

_FRAME_DIVISOR = 10
"""A quiet-moment search frame lasts ``1 / _FRAME_DIVISOR`` s: shorter than a word-final pause."""

_SMOOTHING_FRAMES = 3

_MIN_TAIL_SECONDS = 0.5
"""A trailing remainder shorter than this is dropped; the API rejects very short audio."""


@dataclass(frozen=True)
class Chunk:
    """One upload-sized piece of a recording.

    Attributes:
        start_s: Offset of the chunk's first sample in the source recording, in seconds.
        duration_s: Length of the audio in the chunk, in seconds.
        mp3: The encoded chunk: mono, ``STT_SAMPLERATE`` Hz, ``STT_BITRATE`` kbps.
    """

    start_s: float
    duration_s: float
    mp3: bytes


class ChunkerError(Exception):
    """The recording could not be decoded or turned into uploadable chunks."""


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


def _encode(samples: np.ndarray) -> bytes:
    """Encode mono ``STT_SAMPLERATE`` int16 samples as a self-contained mp3.

    Raises:
        ChunkerError: If the result is larger than ``UPLOAD_LIMIT_BYTES``.
    """
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(STT_BITRATE)
    encoder.set_in_sample_rate(STT_SAMPLERATE)
    encoder.set_channels(1)
    encoder.set_quality(2)
    data = bytes(encoder.encode(samples.astype(np.int16).tobytes()) + encoder.flush())
    if len(data) > UPLOAD_LIMIT_BYTES:
        raise ChunkerError(
            f"a chunk encoded to {len(data)} bytes, over the upload limit of "
            f"{UPLOAD_LIMIT_BYTES} bytes"
        )
    return data


def _pcm_blocks(path: Path) -> Iterator[np.ndarray]:
    """Yield the recording as mono ``STT_SAMPLERATE`` int16 blocks of about one second.

    Raises:
        ChunkerError: If the decoder is missing or the file cannot be decoded.
    """
    try:
        import miniaudio
    except ImportError as exc:
        raise ChunkerError(
            'the miniaudio decoder is not installed; run: pip install -e ".[transcribe]"'
        ) from exc
    try:
        for block in miniaudio.stream_file(
            str(path),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=STT_SAMPLERATE,
            frames_to_read=STT_SAMPLERATE,
        ):
            # A copy: the decoder's array is not ours to keep referencing.
            yield np.array(block, dtype=np.int16)
    except miniaudio.MiniaudioError as exc:
        raise ChunkerError(f"cannot decode {path.name}: {exc}") from exc


def iter_chunks(
    path: Path,
    *,
    target_s: int = CHUNK_TARGET_SECONDS,
    search_s: int = CHUNK_SEARCH_SECONDS,
) -> Iterator[Chunk]:
    """Decode ``path`` and yield it as mono mp3 chunks cut at quiet moments.

    The recording is decoded as a stream and only about ``target_s + search_s`` seconds of PCM
    are held at once, so memory use does not grow with the recording's length. Each chunk is
    encoded on its own, so chunks are independent files.

    Args:
        path: Recording to read; any format miniaudio decodes (mp3, wav, flac, ogg).
        target_s: Audio length after which the search for a cut point begins.
        search_s: How far past ``target_s`` the cut point may be.

    Yields:
        Consecutive, non-overlapping chunks that together cover the recording.

    Raises:
        ChunkerError: If the file cannot be decoded, holds less than half a second of audio, or
            a chunk would exceed the upload limit.
    """
    rate = STT_SAMPLERATE
    target = int(target_s * rate)
    search = int(search_s * rate)
    pending: list[np.ndarray] = []
    buffered = 0
    emitted = 0
    produced = False

    def chunk_from(samples: np.ndarray) -> Chunk:
        """Encode ``samples`` as the chunk that starts after everything emitted so far."""
        return Chunk(start_s=emitted / rate, duration_s=len(samples) / rate, mp3=_encode(samples))

    for block in _pcm_blocks(path):
        pending.append(block)
        buffered += len(block)
        while buffered >= target + search:
            samples = np.concatenate(pending)
            cut = find_quiet_cut(samples, target, search, rate)
            yield chunk_from(samples[:cut])
            produced = True
            emitted += cut
            pending = [samples[cut:]]
            buffered = len(pending[0])

    tail = np.concatenate(pending) if pending else np.zeros(0, dtype=np.int16)
    if len(tail) >= _MIN_TAIL_SECONDS * rate:
        yield chunk_from(tail)
    elif not produced:
        raise ChunkerError(f"no audio found in {path.name}")
