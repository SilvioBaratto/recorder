"""PyAudioWPatch capture layer: device discovery and background per-device readers.

soundcard cannot open some Windows capture devices (it asserts the device's WASAPI mix format
is ``WAVE_FORMAT_EXTENSIBLE`` float32, which headset/communications mics frequently are not).
PyAudioWPatch (PortAudio + WASAPI) opens whatever format the device reports, so it is used for
both the speaker loopback and the microphone.
"""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np
import pyaudiowpatch as pyaudio

from .resample import LinearResampler

Log = Callable[[str], None]

_POLL_SECONDS = 0.01


def open_pyaudio() -> "pyaudio.PyAudio":
    """Return a new PyAudio instance (caller is responsible for ``terminate()``)."""
    return pyaudio.PyAudio()


def find_loopback_device(pa: "pyaudio.PyAudio") -> dict:
    """Return the WASAPI loopback device info for the current default output.

    Raises:
        RuntimeError: If no loopback endpoint matches the default output device.
    """
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    output = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if output.get("isLoopbackDevice"):
        return output
    for loopback in pa.get_loopback_device_info_generator():
        if output["name"] in loopback["name"]:
            return loopback
    raise RuntimeError(f"no loopback endpoint found for default output {output['name']!r}")


def find_input_device(pa: "pyaudio.PyAudio") -> dict:
    """Return the default WASAPI input (microphone) device info."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    return pa.get_device_info_by_index(wasapi["defaultInputDevice"])


def describe_devices(pa: "pyaudio.PyAudio") -> str:
    """Return a human-readable listing of the loopback and input devices."""
    lines = []
    try:
        loopback = find_loopback_device(pa)
        lines.append(f"Call loopback source : {loopback['name']} @ {int(loopback['defaultSampleRate'])} Hz")
    except RuntimeError as exc:
        lines.append(f"Call loopback source : UNAVAILABLE ({exc})")
    mic = find_input_device(pa)
    lines.append(f"Microphone (your voice): {mic['name']} @ {int(mic['defaultSampleRate'])} Hz")
    lines.append("\nAll loopback endpoints:")
    for loopback in pa.get_loopback_device_info_generator():
        lines.append(f"  - {loopback['name']}")
    return "\n".join(lines)


def _to_stereo(block: np.ndarray, channels: int) -> np.ndarray:
    """Coerce an ``(n, channels)`` block to ``(n, 2)`` by duplicating mono or dropping extras."""
    if channels == 1:
        return np.repeat(block, 2, axis=1)
    if channels == 2:
        return block
    return np.ascontiguousarray(block[:, :2])


class DeviceStream:
    """Background reader that yields stereo float32 frames at a common target rate.

    A worker thread reads native blocks from one WASAPI device, converts them to stereo,
    resamples to ``target_rate``, and appends to an internal buffer. :meth:`pull` hands out
    fixed-size chunks to the mixer, blocking until enough frames exist or the stream ends.
    """

    def __init__(self, pa, device_info: dict, target_rate: int, stop_event: threading.Event,
                 log: Log, label: str) -> None:
        self.label = label
        self._stop = stop_event
        self._log = log
        self._channels = int(device_info["maxInputChannels"])
        self._native_rate = int(device_info["defaultSampleRate"])
        self._native_frames = max(256, self._native_rate // 10)
        self._resampler = LinearResampler(self._native_rate, target_rate, channels=2)
        self._buf = np.zeros((0, 2), dtype=np.float32)
        self._max_buffer = target_rate * 10  # drop oldest beyond ~10 s to bound latency/memory
        self._cond = threading.Condition()
        self._finished = False
        self._stream = pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=self._native_rate,
            frames_per_buffer=self._native_frames,
            input=True,
            input_device_index=device_info["index"],
        )
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Begin reading from the device on the worker thread."""
        self._thread.start()

    def _run(self) -> None:
        """Read, convert, and resample native blocks until stopped or the device errors."""
        try:
            while not self._stop.is_set():
                # Never block in read(): a loopback endpoint delivers nothing while the call is
                # silent, and a reader still parked in PortAudio at teardown crashes the process.
                available = self._stream.get_read_available()
                if available == 0:
                    self._stop.wait(_POLL_SECONDS)
                    continue
                raw = self._stream.read(available, exception_on_overflow=False)
                native = np.frombuffer(raw, dtype=np.float32).reshape(-1, self._channels)
                frames = self._resampler.process(_to_stereo(native, self._channels))
                if len(frames):
                    with self._cond:
                        self._buf = np.concatenate([self._buf, frames])
                        if len(self._buf) > self._max_buffer:
                            self._buf = self._buf[-self._max_buffer:]
                        self._cond.notify_all()
        except Exception as exc:  # noqa: BLE001 - surfaced via log; triggers clean shutdown
            self._log(f"{self.label} capture error: {exc!r}")
            self._stop.set()
        finally:
            with self._cond:
                self._finished = True
                self._cond.notify_all()
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def pull(self, frames: int) -> np.ndarray:
        """Return up to ``frames`` buffered frames, blocking until available or finished.

        Returns fewer than ``frames`` (possibly zero) once the stream has finished, the stop
        signal is set, or its buffer is drained — which is how the mixer learns to stop. The
        stop check also unblocks the mixer when a device stops delivering frames.
        """
        with self._cond:
            while len(self._buf) < frames and not self._finished and not self._stop.is_set():
                self._cond.wait(timeout=0.5)
            take = min(frames, len(self._buf))
            out = self._buf[:take]
            self._buf = self._buf[take:]
            return out

    def pull_nowait(self, frames: int) -> np.ndarray:
        """Return up to ``frames`` buffered frames immediately, without blocking.

        Used for the loopback in the mic-driven loop: take whatever call audio is ready now
        (possibly none) to mix against the current mic block.
        """
        with self._cond:
            take = min(frames, len(self._buf))
            out = self._buf[:take]
            self._buf = self._buf[take:]
            return out

    def join(self, timeout: float = 2.0) -> None:
        """Wait for the worker thread to exit."""
        self._thread.join(timeout=timeout)
