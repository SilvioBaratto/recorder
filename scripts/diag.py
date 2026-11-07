"""Diagnostic: record the loopback and the mic separately and compare them.

Run it while playing a YouTube video and speaking. It captures both devices for a few
seconds into separate files and prints per-stream energy plus how similar the two streams
are — revealing whether system audio reaches the loopback and whether your voice leaks into
both streams (which would explain an echo after mixing).
"""

from __future__ import annotations

import threading
import time

import numpy as np

from recorder.audio_io import DeviceStream, find_input_device, find_loopback_device, open_pyaudio
from recorder.config import BLOCK, SAMPLERATE
from recorder.state import audio_dir

DURATION_SECONDS = 6.0


def _collect(stream: DeviceStream, blocks: list, count: int) -> None:
    """Pull ``count`` blocks from ``stream`` into ``blocks``."""
    for _ in range(count):
        blocks.append(stream.pull(BLOCK))


def _to_array(blocks: list) -> np.ndarray:
    """Concatenate captured blocks into one float32 array, or a single silent frame."""
    usable = [b for b in blocks if len(b)]
    return np.concatenate(usable) if usable else np.zeros((1, 2), dtype=np.float32)


def _save_mp3(path, data: np.ndarray) -> None:
    """Encode a float32 stereo array to a 128 kbps mp3 at ``path``."""
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(128)
    encoder.set_in_sample_rate(SAMPLERATE)
    encoder.set_channels(2)
    encoder.set_quality(2)
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with open(path, "wb") as handle:
        handle.write(encoder.encode(pcm))
        handle.write(encoder.flush())


def _rms(mono: np.ndarray) -> float:
    """Return the RMS level of a mono signal (0.0 for empty)."""
    return float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0


def _best_correlation(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Return the peak normalized correlation of two mono signals and its lag in ms.

    Searches lags up to +/-50 ms; a high value means the two streams carry the same sound
    (one signal captured twice), which is what produces an echo after mixing.
    """
    n = min(len(a), len(b))
    a = a[:n] - np.mean(a[:n])
    b = b[:n] - np.mean(b[:n])
    denom = np.sqrt(np.sum(a ** 2) * np.sum(b ** 2))
    if denom == 0:
        return 0.0, 0.0
    best, best_lag = 0.0, 0
    step = SAMPLERATE // 1000
    for lag in range(-50 * step, 50 * step + 1, step):
        if lag >= 0:
            corr = np.dot(a[lag:], b[: n - lag])
        else:
            corr = np.dot(a[: n + lag], b[-lag:])
        value = corr / denom
        if abs(value) > abs(best):
            best, best_lag = value, lag
    return best, best_lag * 1000 / SAMPLERATE


def main() -> int:
    """Capture both devices concurrently, print a comparison, and save two mp3s."""
    pa = open_pyaudio()
    stop = threading.Event()
    try:
        loop_info = find_loopback_device(pa)
        mic_info = find_input_device(pa)
        print(f"loopback source : {loop_info['name']!r}")
        print(f"microphone      : {mic_info['name']!r}")

        loop_stream = DeviceStream(pa, loop_info, SAMPLERATE, stop, lambda m: print(m), "loopback")
        mic_stream = DeviceStream(pa, mic_info, SAMPLERATE, stop, lambda m: print(m), "mic")
        loop_stream.start()
        mic_stream.start()

        blocks = int(DURATION_SECONDS * SAMPLERATE / BLOCK)
        loop_blocks: list = []
        mic_blocks: list = []
        collectors = [
            threading.Thread(target=_collect, args=(loop_stream, loop_blocks, blocks)),
            threading.Thread(target=_collect, args=(mic_stream, mic_blocks, blocks)),
        ]
        print(f"\n>>> Recording {DURATION_SECONDS:.0f}s - PLAY A YOUTUBE VIDEO and SPEAK NOW <<<\n")
        start = time.time()
        for thread in collectors:
            thread.start()
        for thread in collectors:
            thread.join()
        stop.set()
        loop_stream.join()
        mic_stream.join()
        print(f"(captured {time.time() - start:.1f}s)")

        loop = _to_array(loop_blocks)
        mic = _to_array(mic_blocks)
    finally:
        pa.terminate()

    audio_dir().mkdir(parents=True, exist_ok=True)
    loop_path = audio_dir() / "_diag_loopback.mp3"
    mic_path = audio_dir() / "_diag_mic.mp3"
    _save_mp3(loop_path, loop)
    _save_mp3(mic_path, mic)

    corr, lag_ms = _best_correlation(loop[:, 0], mic[:, 0])
    print(f"loopback  RMS={_rms(loop[:, 0]):.4f}  peak={float(np.abs(loop).max()):.4f}")
    print(f"mic       RMS={_rms(mic[:, 0]):.4f}  peak={float(np.abs(mic).max()):.4f}")
    print(f"similarity(loopback, mic) = {corr:+.2f} at lag {lag_ms:+.0f} ms")
    print("\nListen to both to hear what each stream captured:")
    print(f"  {loop_path}")
    print(f"  {mic_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
