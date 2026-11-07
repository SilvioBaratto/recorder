"""Transcribe a recording with faster-whisper (local, no API key, no ffmpeg binary needed).

Usage: ``python scripts/transcribe.py <audio-file> [model-size]`` (default model ``base``).
"""

from __future__ import annotations

import sys

from faster_whisper import WhisperModel


def main() -> int:
    """Transcribe the given audio file and print detected language plus timed segments."""
    if len(sys.argv) < 2:
        print("usage: python scripts/transcribe.py <audio-file> [model-size]")
        return 2
    path = sys.argv[1]
    model_size = sys.argv[2] if len(sys.argv) > 2 else "base"

    print(f"loading faster-whisper '{model_size}' (first run downloads the model)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(path, vad_filter=True)

    print(f"language = {info.language} (p={info.language_probability:.2f})\n")
    any_text = False
    for seg in segments:
        any_text = True
        print(f"[{seg.start:6.1f}s - {seg.end:6.1f}s] {seg.text.strip()}")
    if not any_text:
        print("(no speech detected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
