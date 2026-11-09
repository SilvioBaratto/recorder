"""Core Audio backend: a ``catap`` system-audio tap for the call and ``sounddevice`` for the mic.

A process tap (macOS 14.2+) captures the system output mix without a virtual audio device,
so nothing has to be installed or rerouted. ``catap`` and ``sounddevice`` are imported only
when a device is touched, which keeps this module importable on every operating system.
"""

from __future__ import annotations

import importlib.util
import platform
import sys
import threading
from typing import Any, Callable, Optional, Tuple

import numpy as np

from .base import OnBlock, OnEnd, Source, SourceInfo
from .polled import PolledSource

_TAP_NAME = "System audio (Core Audio tap)"
_MIN_MACOS = (14, 2)
_MIN_PYTHON = (3, 11)
_MAX_MIC_CHANNELS = 2
_WATCH_SECONDS = 0.1
_JOIN_SECONDS = 2.0

# catap refuses big-endian taps, so every layout here is little-endian.
_SAMPLE_LAYOUTS = {
    ("float", 32): ("<f4", 1.0),
    ("signed_integer", 16): ("<i2", 1.0 / 32768),
    ("signed_integer", 32): ("<i4", 1.0 / 2147483648),
}

_PERMISSIONS = "System Settings > Privacy & Security"
_NO_MICROPHONE = ("no microphone available ({}): choose an input device in System Settings > "
                  "Sound > Input, and grant your terminal app Microphone access in "
                  + _PERMISSIONS + " > Microphone")
_PERMISSION_REMINDER = ("Grant your terminal app System Audio Recording and Microphone access in "
                        + _PERMISSIONS + ", then restart it.")
# catap only raises a flag here; its detailed reason surfaces from stop(), after this message has
# already ended the stream, so the message itself names the causes catap documents.
_CAPTURE_FAILED = ("Core Audio reported a system-audio capture failure while recording; likely "
                   "causes: the output device or route changed, the stream format changed, the "
                   "tap was destroyed, or more than 256 audio buffers queued up unprocessed. "
                   "Check the devices with 'recorder devices'")


def check_preconditions(mac_version: Tuple[int, ...], python_version: Tuple[int, ...],
                        catap_available: bool) -> None:
    """Check that system-audio capture can work here, most fundamental problem first.

    Args:
        mac_version: Release of the running macOS, for example ``(15, 1)``; empty when unknown.
        python_version: ``(major, minor)`` of the running interpreter.
        catap_available: Whether the ``catap`` package can be imported.

    Raises:
        RuntimeError: Naming the fix: macOS older than 14.2, Python older than 3.11 (catap
            does not install there), or catap missing.
    """
    if tuple(mac_version) < _MIN_MACOS:
        detected = ".".join(str(part) for part in mac_version) or "unknown"
        raise RuntimeError("capturing call audio needs macOS 14.2 or later (this is macOS {}); "
                           "update macOS in System Settings > General > Software Update"
                           .format(detected))
    if tuple(python_version[:2]) < _MIN_PYTHON:
        raise RuntimeError("capturing call audio on macOS needs Python 3.11 or later (running {}); "
                           "reinstall recorder with a Python 3.11+ interpreter"
                           .format(".".join(str(part) for part in python_version[:2])))
    if not catap_available:
        raise RuntimeError("the catap package is missing; install it with: pip install catap")


def convert_buffer(data: bytes, sample_type: str, bits: int, channels: int) -> np.ndarray:
    """Turn one interleaved Core Audio buffer into float32 frames.

    Args:
        data: Interleaved little-endian PCM samples.
        sample_type: ``"float"`` or ``"signed_integer"``, as catap reports it.
        bits: Bits per sample.
        channels: Interleaved channel count.

    Returns:
        A new, writable float32 array shaped ``(frames, channels)`` scaled to [-1, 1]. A
        trailing partial frame is dropped.

    Raises:
        ValueError: If the format is not 32-bit float or 16/32-bit signed integer, or
            ``channels`` is not positive.
    """
    layout = _SAMPLE_LAYOUTS.get((sample_type, bits))
    if layout is None:
        raise ValueError("unsupported Core Audio sample format: {}-bit {}".format(bits, sample_type))
    if channels < 1:
        raise ValueError("unsupported Core Audio channel count: {}".format(channels))
    dtype, scale = layout
    sample_bytes = bits // 8
    frames = len(data) // (sample_bytes * channels)
    samples = np.frombuffer(data, dtype=dtype, count=frames * channels)
    block = samples.astype(np.float32)
    if scale != 1.0:
        block *= np.float32(scale)
    return block.reshape(frames, channels)


class CoreAudioTapSource:
    """Streams the system output mix from a ``catap`` process tap.

    The tap is already running once the source exists: catap reports the native stream
    format only after the tap starts, and ``info`` must be known before ``start``.

    Attributes:
        info: Native format and name of the tap.
    """

    def __init__(self, record_system_audio: Callable[..., Any]) -> None:
        """Create and start the tap, then read its native format.

        Args:
            record_system_audio: ``catap.record_system_audio``; tests inject a fake.

        Raises:
            RuntimeError: If Core Audio refuses the tap or it reports no usable format; the
                tap is released before raising.
        """
        self._state_lock = threading.Lock()
        # Held across on_block so on_end can never overlap or precede a block in flight.
        self._deliver_lock = threading.Lock()
        self._closing = threading.Event()
        self._in_callback = threading.local()
        self._on_block: Optional[OnBlock] = None
        self._on_end: Optional[OnEnd] = None
        self._ended = False
        self._closed = False
        self._watcher: Optional[threading.Thread] = None
        session = record_system_audio(on_buffer=self._forward, mute=False)
        try:
            session.start()
        except Exception as exc:  # noqa: BLE001 - catap also raises ValueError/RuntimeError subclasses
            _close_quietly(session)
            raise RuntimeError("could not start the Core Audio system-audio tap: {}".format(exc)) from exc
        try:
            self.info = _tap_info(session.stream_format)
        except BaseException:
            _stop_session(session)
            raise
        self._session = session

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Begin forwarding tap buffers and watching the tap for failures.

        Args:
            on_block: Called with each float32 ``(n, channels)`` block, from catap's worker
                thread.
            on_end: Called exactly once. After ``close`` it runs on the closing thread (a
                helper thread when closed from inside a callback) and receives ``None``, or
                the error stopping the tap raised. It receives a ``RuntimeError`` from a
                watcher thread if catap reports a capture failure, and the exception from
                catap's worker thread if conversion or ``on_block`` raised. No block is
                delivered once it has been called.

        Raises:
            RuntimeError: If the source was closed or started before.
        """
        with self._state_lock:
            if self._closed:
                raise RuntimeError("source is closed")
            if self._watcher is not None:
                raise RuntimeError("source already started")
            with self._deliver_lock:
                self._on_end = on_end
                self._on_block = on_block
            watcher = threading.Thread(target=self._watch, name="core-audio-tap-watch", daemon=True)
            self._watcher = watcher
            try:
                watcher.start()
            except BaseException:
                # Undone so close() neither joins a thread that never ran nor reports an end
                # for a start the caller saw fail.
                self._watcher = None
                with self._deliver_lock:
                    self._on_block = None
                    self._on_end = None
                raise

    def close(self) -> None:
        """Stop the tap and end the stream. Idempotent; safe from inside ``on_block``/``on_end``."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._closing.set()
            watcher = self._watcher
        if getattr(self._in_callback, "active", False):
            # catap's stop() joins the worker running this callback, so stopping here would
            # wait on itself; the teardown moves to a thread that can wait for it.
            threading.Thread(target=self._teardown, args=(watcher,), name="core-audio-tap-close",
                             daemon=True).start()
            return
        self._teardown(watcher)

    def _forward(self, buffer: Any) -> None:
        self._in_callback.active = True
        try:
            failure: Optional[BaseException] = None
            with self._deliver_lock:
                on_block = self._on_block
                # The tap runs from open time to learn its format, so buffers that arrive
                # before start() have no consumer yet and are dropped.
                if on_block is None or self._ended or self._closing.is_set():
                    return
                try:
                    fmt = buffer.format
                    on_block(convert_buffer(buffer.data, fmt.sample_type, fmt.bits_per_sample,
                                            fmt.num_channels))
                except BaseException as exc:
                    # Raised into catap, this would silence the callback for good and surface
                    # only as an anonymous capture failure; on_end gets the real cause instead.
                    failure = exc
            if failure is not None:
                self._end(failure)
        finally:
            self._in_callback.active = False

    def _watch(self) -> None:
        while not self._closing.wait(_WATCH_SECONDS):
            if self._session.capture_failed:
                self._end(RuntimeError(_CAPTURE_FAILED))
                return

    def _teardown(self, watcher: Optional[threading.Thread]) -> None:
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(_JOIN_SECONDS)
        self._end(_stop_session(self._session))

    def _end(self, error: Optional[BaseException]) -> None:
        with self._deliver_lock:
            if self._ended or self._on_end is None:
                return
            self._ended = True
            on_end = self._on_end
        on_end(error)


def _tap_info(stream_format: Any) -> SourceInfo:
    if stream_format is None:
        raise RuntimeError("the Core Audio tap started without reporting its stream format")
    try:
        convert_buffer(b"", stream_format.sample_type, stream_format.bits_per_sample,
                       stream_format.num_channels)
    except ValueError as exc:
        raise RuntimeError("the Core Audio tap delivers an unsupported format: {}".format(exc)) from exc
    return SourceInfo(name=_TAP_NAME, sample_rate=int(round(stream_format.sample_rate)),
                      channels=int(stream_format.num_channels))


def _stop_session(session: Any) -> Optional[BaseException]:
    error: Optional[BaseException] = None
    try:
        session.stop()
    except Exception as exc:  # noqa: BLE001 - the caller reports it through on_end
        error = exc
    # A failed teardown keeps the tap alive until close() succeeds.
    if session.needs_cleanup:
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001 - the caller reports it through on_end
            error = error or exc
    return error


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001 - the start failure is the error worth raising
        pass


def _parse_release(release: str) -> Tuple[int, ...]:
    parts = []
    for part in release.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts)


class MacBackend:
    """Records the system output through a Core Audio tap and the default input via PortAudio.

    Attributes:
        silence_hint: Shown when the call side stayed silent. macOS hands a process without
            the System Audio Recording permission zeroed tap buffers instead of an error, so a
            silent call side is the only symptom of the missing permission.
    """

    silence_hint = ("The call side was silent for the whole recording. If the call was audible, "
                    "macOS withheld the system audio: grant your terminal app System Audio "
                    "Recording access in " + _PERMISSIONS + ", then quit and restart the "
                    "terminal app, because macOS applies the permission only after a restart.")

    def __init__(self, catap_module: Any = None, sounddevice_module: Any = None,
                 mac_version: Optional[Tuple[int, ...]] = None,
                 python_version: Optional[Tuple[int, ...]] = None) -> None:
        """Prepare the backend without importing or touching any audio library.

        Args:
            catap_module: Module providing ``record_system_audio``; ``None`` imports ``catap``
                on first use. Tests inject a fake here.
            sounddevice_module: Module providing ``query_devices`` and ``InputStream``; ``None``
                imports ``sounddevice`` on first use.
            mac_version: macOS release to check; ``None`` reads ``platform.mac_ver()``.
            python_version: Interpreter version to check; ``None`` reads ``sys.version_info``.
        """
        self._catap_module = catap_module
        self._sounddevice_module = sounddevice_module
        self._mac_version = (tuple(mac_version) if mac_version is not None
                             else _parse_release(platform.mac_ver()[0]))
        self._python_version = (tuple(python_version) if python_version is not None
                                else tuple(sys.version_info[:2]))

    def open_loopback(self) -> Source:
        """Start a streaming Core Audio tap on the system output mix.

        Returns:
            A source whose tap is already running; blocks reach ``on_block`` only after
            ``start``.

        Raises:
            RuntimeError: If macOS, Python or catap do not support the tap, Core Audio refuses
                it, or it delivers a format this backend cannot convert.
        """
        return CoreAudioTapSource(self._catap().record_system_audio)

    def open_microphone(self) -> Source:
        """Open the default input device at its native rate, with at most two channels.

        Returns:
            An unstarted source that polls the input stream.

        Raises:
            RuntimeError: If there is no usable input device or it cannot be opened.
        """
        name, rate, channels = self._input_device()
        sd = self._sounddevice()
        try:
            stream = sd.InputStream(samplerate=rate, channels=channels, dtype="float32")
        except Exception as exc:  # noqa: BLE001 - PortAudioError is not a RuntimeError
            raise RuntimeError("could not open the microphone {!r} ({}); grant your terminal app "
                               "Microphone access in {} > Microphone"
                               .format(name, exc, _PERMISSIONS)) from exc

        def close_device() -> None:
            """Stop, then close the stream even if stopping failed."""
            try:
                stream.stop()
            finally:
                stream.close()

        return PolledSource(
            SourceInfo(name=name, sample_rate=rate, channels=channels),
            available=lambda: stream.read_available,
            read=lambda frames: stream.read(frames)[0],
            close_device=close_device,
            start_device=stream.start,
        )

    def describe(self) -> str:
        """Return a human-readable summary of the tap, the microphone and the permissions.

        Never raises and never starts a capture: each problem is rendered inline as
        ``UNAVAILABLE (<reason>)``.
        """
        return "\n".join([
            self._describe_line("Call loopback source :", self._loopback_label),
            self._describe_line("Microphone (your voice):", self._microphone_label),
            "",
            _PERMISSION_REMINDER,
        ])

    def close(self) -> None:
        """Do nothing: each source owns its tap or stream."""

    @staticmethod
    def _describe_line(label: str, resolve: Callable[[], str]) -> str:
        try:
            return "{} {}".format(label, resolve())
        except Exception as exc:  # noqa: BLE001 - describe() must explain, never crash
            return "{} UNAVAILABLE ({})".format(label, exc)

    def _loopback_label(self) -> str:
        self._catap()
        return _TAP_NAME

    def _microphone_label(self) -> str:
        name, rate, _ = self._input_device()
        return "{} @ {} Hz".format(name, rate)

    def _catap(self) -> Any:
        available = self._catap_module is not None or importlib.util.find_spec("catap") is not None
        check_preconditions(self._mac_version, self._python_version, available)
        if self._catap_module is None:
            try:
                import catap
            except ImportError as exc:
                raise RuntimeError("catap is installed but cannot be imported ({}); reinstall it "
                                   "with: pip install catap".format(exc)) from exc
            self._catap_module = catap
        return self._catap_module

    def _sounddevice(self) -> Any:
        if self._sounddevice_module is None:
            try:
                import sounddevice
            except (ImportError, OSError) as exc:
                raise RuntimeError("sounddevice is unavailable ({}); install it with: "
                                   "pip install sounddevice".format(exc)) from exc
            self._sounddevice_module = sounddevice
        return self._sounddevice_module

    def _input_device(self) -> Tuple[str, int, int]:
        sd = self._sounddevice()
        try:
            device = sd.query_devices(kind="input")
        except Exception as exc:  # noqa: BLE001 - PortAudioError is not a RuntimeError
            raise RuntimeError(_NO_MICROPHONE.format(exc)) from exc
        # The mixer keeps two channels, so a multichannel interface would only stream and
        # convert inputs that are dropped right after.
        channels = min(int(device["max_input_channels"]), _MAX_MIC_CHANNELS)
        if channels < 1:
            raise RuntimeError(_NO_MICROPHONE.format("the default input has no channels"))
        return str(device["name"]), int(round(device["default_samplerate"])), channels
