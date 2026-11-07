"""Capture orchestration: read the loopback and mic, mix them, and stream mp3 to disk."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from . import state
from .audio_io import DeviceStream, find_input_device, find_loopback_device, open_pyaudio
from .config import BITRATE
from .mix import fit_and_mix

Log = Callable[[str], None]


class NoAudioCaptured(RuntimeError):
    """Raised when the microphone produced no frames, so there is nothing to save."""


def _mic_gain() -> float:
    """Return the mic gain from ``RECORDER_MIC_GAIN`` (default 1.0, clamped to [0.1, 10])."""
    try:
        value = float(os.environ.get("RECORDER_MIC_GAIN", "1.0"))
    except ValueError:
        return 1.0
    return max(0.1, min(value, 10.0))


def record(output: Path, stop_event: threading.Event, log: Log, samplerate: int, blocksize: int) -> int:
    """Capture the call and microphone, mix them, and save the result to ``output`` as mp3.

    Opens the speaker loopback and the default microphone through PyAudioWPatch, resamples both
    to ``samplerate``, pairs their frames block-for-block, mixes, and streams mp3 into
    ``output``'s ``.part`` sibling. Blocks until ``stop_event`` is set and both streams drain,
    then publishes the file as ``output`` before the audio devices are torn down.

    Args:
        output: Final mp3 path; it appears only once the recording is complete.
        stop_event: Set by the daemon when the stop sentinel appears; ends capture.
        log: Sink for diagnostic lines.
        samplerate: Common rate the two streams are resampled to and the mp3 is encoded at.
        blocksize: Frames mixed and encoded per step.

    Returns:
        Total frames written, from which duration is ``frames / samplerate``.

    Raises:
        NoAudioCaptured: If the microphone yielded no audio; nothing is saved.
    """
    import lameenc

    partial = state.partial_path(output)
    pa = open_pyaudio()
    try:
        loopback_info = find_loopback_device(pa)
        mic_info = find_input_device(pa)
        log(f"loopback = {loopback_info['name']!r} @ {int(loopback_info['defaultSampleRate'])} Hz")
        log(f"microphone = {mic_info['name']!r} @ {int(mic_info['defaultSampleRate'])} Hz")

        loop_stream = DeviceStream(pa, loopback_info, samplerate, stop_event, log, "loopback")
        mic_stream = DeviceStream(pa, mic_info, samplerate, stop_event, log, "mic")
        loop_stream.start()
        mic_stream.start()

        encoder = lameenc.Encoder()
        encoder.set_bit_rate(BITRATE)
        encoder.set_in_sample_rate(samplerate)
        encoder.set_channels(2)
        encoder.set_quality(2)

        mic_gain = _mic_gain()
        if mic_gain != 1.0:
            log(f"mic gain = {mic_gain:.2f}")

        frames = 0
        heartbeat = max(1, (samplerate // blocksize) * 10)
        block_index = 0
        try:
            with open(partial, "wb") as handle:
                while True:
                    mic_block = mic_stream.pull(blocksize)
                    if len(mic_block) == 0:
                        if stop_event.is_set():
                            break
                        continue
                    loop_block = loop_stream.pull_nowait(len(mic_block))
                    mixed = fit_and_mix(loop_block, mic_block, mic_gain)
                    handle.write(encoder.encode((mixed * 32767.0).astype("<i2").tobytes()))
                    frames += len(mic_block)
                    block_index += 1
                    if block_index % heartbeat == 0:
                        log(f"recording {frames / samplerate:6.1f}s peak={float(np.abs(mixed).max()):.3f}")
                if frames:
                    handle.write(encoder.flush())
            # Publish before PortAudio teardown: closing a device while a reader thread is still
            # blocked inside it crashes the process, which must not take the recording with it.
            if state.publish_partial(output):
                log(f"saved {output} duration={frames / samplerate:.1f}s")
        finally:
            stop_event.set()
            loop_stream.join()
            mic_stream.join()

        if frames == 0:
            raise NoAudioCaptured("microphone produced no audio")
        return frames
    finally:
        pa.terminate()
