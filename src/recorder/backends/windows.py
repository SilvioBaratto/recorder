"""PyAudioWPatch backend: WASAPI speaker loopback and default microphone on Windows.

soundcard cannot open some Windows capture devices (it asserts the device's WASAPI mix format
is ``WAVE_FORMAT_EXTENSIBLE`` float32, which headset/communications mics frequently are not).
PyAudioWPatch (PortAudio + WASAPI) opens whatever format the device reports, so it is used for
both the speaker loopback and the microphone.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .base import Source, SourceInfo
from .polled import PolledSource


class WindowsBackend:
    """Opens WASAPI devices through PortAudio and reads them with a :class:`PolledSource`.

    PortAudio is started lazily, on the first device query, so constructing the backend is
    free and ``pyaudiowpatch`` is only imported where it is installed.

    Attributes:
        silence_hint: Empty, since WASAPI loopback needs no OS permission.
    """

    silence_hint = ""

    def __init__(self, pyaudio_module: Any = None) -> None:
        """Prepare the backend without touching PortAudio.

        Args:
            pyaudio_module: Module providing ``PyAudio``, ``paFloat32`` and ``paWASAPI``;
                ``None`` imports ``pyaudiowpatch`` on first use. Tests inject a fake here.
        """
        self._module = pyaudio_module
        self._pa: Any = None

    def _portaudio(self) -> Any:
        if self._pa is None:
            if self._module is None:
                import pyaudiowpatch

                self._module = pyaudiowpatch
            self._pa = self._module.PyAudio()
        return self._pa

    def find_loopback_device(self) -> dict:
        """Return the WASAPI loopback device info for the current default output.

        Raises:
            RuntimeError: If no loopback endpoint matches the default output device.
        """
        pa = self._portaudio()
        wasapi = pa.get_host_api_info_by_type(self._module.paWASAPI)
        output = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if output.get("isLoopbackDevice"):
            return output
        for loopback in self._loopback_devices():
            if output["name"] in loopback["name"]:
                return loopback
        raise RuntimeError(f"no loopback endpoint found for default output {output['name']!r}")

    def find_input_device(self) -> dict:
        """Return the default WASAPI input (microphone) device info."""
        pa = self._portaudio()
        wasapi = pa.get_host_api_info_by_type(self._module.paWASAPI)
        return pa.get_device_info_by_index(wasapi["defaultInputDevice"])

    def _loopback_devices(self) -> Iterable[dict]:
        return self._portaudio().get_loopback_device_info_generator()

    def open_loopback(self) -> Source:
        """Open the loopback endpoint of the default output device.

        Returns:
            An unstarted source delivering the call audio at the device's native format.

        Raises:
            RuntimeError: If no loopback endpoint matches the default output device.
        """
        return self._open(self.find_loopback_device())

    def open_microphone(self) -> Source:
        """Open the default WASAPI input device.

        Returns:
            An unstarted source delivering the microphone at the device's native format.
        """
        return self._open(self.find_input_device())

    def _open(self, device_info: dict) -> PolledSource:
        pa = self._portaudio()
        channels = int(device_info["maxInputChannels"])
        rate = int(device_info["defaultSampleRate"])
        try:
            stream = pa.open(
                format=self._module.paFloat32,
                channels=channels,
                rate=rate,
                frames_per_buffer=max(256, rate // 10),
                input=True,
                input_device_index=device_info["index"],
            )
        except OSError as exc:
            # The engine logs a device's name only after it opened, so without this a refusal
            # would not say which device it was.
            raise OSError(
                "could not open {!r} ({} Hz, {} channels): {}".format(
                    device_info["name"], rate, channels, exc)
            ) from exc

        def read(frames: int) -> np.ndarray:
            """Return ``frames`` already-buffered frames shaped ``(frames, channels)``."""
            raw = stream.read(frames, exception_on_overflow=False)
            return np.frombuffer(raw, dtype=np.float32).reshape(-1, channels)

        def close_device() -> None:
            """Stop and close the stream, ignoring errors from a device already gone."""
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

        # The polling reader matters here: a loopback endpoint delivers nothing while the call
        # is silent, so a blocking read() would park the thread in PortAudio until teardown,
        # and terminating PortAudio under a parked reader crashes the process.
        return PolledSource(
            SourceInfo(name=device_info["name"], sample_rate=rate, channels=channels),
            available=stream.get_read_available,
            read=read,
            close_device=close_device,
        )

    def describe(self) -> str:
        """Return a human-readable listing of the loopback and input devices."""
        # Started outside the try below so a PortAudio start-up failure propagates instead of
        # being listed as an unavailable loopback.
        self._portaudio()
        lines = []
        try:
            loopback = self.find_loopback_device()
            lines.append(f"Call loopback source : {loopback['name']} @ {int(loopback['defaultSampleRate'])} Hz")
        except RuntimeError as exc:
            lines.append(f"Call loopback source : UNAVAILABLE ({exc})")
        mic = self.find_input_device()
        lines.append(f"Microphone (your voice): {mic['name']} @ {int(mic['defaultSampleRate'])} Hz")
        lines.append("\nAll loopback endpoints:")
        for loopback in self._loopback_devices():
            lines.append(f"  - {loopback['name']}")
        return "\n".join(lines)

    def close(self) -> None:
        """Terminate PortAudio if it was started. Idempotent."""
        pa, self._pa = self._pa, None
        if pa is not None:
            pa.terminate()
