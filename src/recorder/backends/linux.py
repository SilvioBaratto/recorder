"""PulseAudio/PipeWire backend: the default sink's monitor and the default microphone via ``parec``.

PipeWire is reached through ``pipewire-pulse``, so one code path serves both sound servers.
``parec`` runs as a subprocess, which keeps Python audio bindings out of the Linux install and
lets the server convert and resample every stream to 48 kHz stereo float32 itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Any, Callable, List, Optional, Tuple

import numpy as np

from ..config import SAMPLERATE
from .base import OnBlock, OnEnd, Source, SourceInfo

_CHANNELS = 2
_FRAME_BYTES = 4 * _CHANNELS
_READ_BYTES = 65536
_TERMINATE_WAIT_SECONDS = 1.0
_JOIN_SECONDS = 2.0
_MONITOR_SUFFIX = ".monitor"

_MISSING_TOOLS = ("{} not found: install the pulseaudio-utils package "
                  "(libpulse on Arch Linux)")


def parse_pactl_info(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract the default output and input names from ``pactl info`` output.

    Args:
        text: English ``pactl info`` output; other locales translate the labels.

    Returns:
        ``(default_sink, default_source)``. Either is ``None`` when its line is absent,
        empty or ``n/a``, which is how a server without that device reports it.
    """
    values = {}
    for line in text.splitlines():
        label, _, value = line.partition(":")
        values[label.strip()] = value.strip()
    return _usable(values.get("Default Sink")), _usable(values.get("Default Source"))


def _usable(value: Optional[str]) -> Optional[str]:
    return value if value and value != "n/a" else None


class ParecSource:
    """Streams one PulseAudio source from a ``parec`` subprocess.

    ``parec`` is spawned by ``start``, so a source that is opened and never started costs
    nothing and ``close`` on it is a no-op.

    Attributes:
        info: Native format and name of the stream; always 48 kHz stereo.
    """

    def __init__(self, device: str, spawn: Callable[..., Any] = subprocess.Popen) -> None:
        """Describe the stream without starting ``parec``.

        Args:
            device: PulseAudio source name, for example ``<sink>.monitor`` or a microphone.
            spawn: ``subprocess.Popen`` replacement; tests inject a fake process here.
        """
        self.info = SourceInfo(name=device, sample_rate=SAMPLERATE, channels=_CHANNELS)
        self._spawn = spawn
        self._closing = threading.Event()
        # Re-entrant because start() reaches _stop_process while holding it.
        self._lock = threading.RLock()
        self._proc: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Spawn ``parec`` and deliver its audio from a daemon thread.

        Args:
            on_block: Called with each ``(n, 2)`` float32 block, from the reader thread.
            on_end: Called exactly once, from the reader thread, after ``parec`` is gone.
                Receives ``None`` after ``close``, otherwise the error that stopped delivery
                (``parec`` exiting by itself, or an exception from ``on_block``).

        Raises:
            RuntimeError: If the source was closed or started before, or ``parec`` cannot run.
        """
        argv = [
            "parec",
            f"--device={self.info.name}",
            "--format=float32le",
            f"--rate={SAMPLERATE}",
            f"--channels={_CHANNELS}",
            "--latency-msec=100",
        ]
        with self._lock:
            if self._closing.is_set():
                raise RuntimeError("source is closed")
            if self._proc is not None:
                raise RuntimeError("source already started")
            try:
                # Unbuffered on purpose: a raw pipe hands over whatever has arrived, while a
                # buffered reader would block until the whole read size is available.
                proc = self._spawn(argv, bufsize=0, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as exc:
                raise RuntimeError(f"could not run parec: {exc}") from exc
            thread = threading.Thread(
                target=self._run,
                args=(proc, on_block, on_end),
                name=f"parec-{self.info.name}",
                daemon=True,
            )
            # Published under the lock, before the thread runs, so a close() from its first
            # callback sees a live process and thread.
            self._proc = proc
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                self._stop_process(proc)
                raise

    def close(self) -> None:
        """Stop ``parec`` and wait briefly for the reader to finish. Idempotent.

        Escalates from SIGTERM to SIGKILL, so it returns even if ``parec`` ignores the
        first. Safe to call from the reader thread itself, where it does not join.
        """
        with self._lock:
            if self._closing.is_set():
                return
            self._closing.set()
            proc, thread = self._proc, self._thread
        if proc is None:
            return
        self._stop_process(proc)
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_SECONDS)

    def _stop_process(self, proc: Any) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        proc.terminate()
        try:
            proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                # A process in uninterruptible sleep outlives SIGKILL; close() must not hang on it.
                pass

    def _run(self, proc: Any, on_block: OnBlock, on_end: OnEnd) -> None:
        error: Optional[BaseException] = None
        try:
            self._pump(proc.stdout, on_block)
            if not self._closing.is_set():
                error = self._exit_error(proc)
        except BaseException as exc:
            # Anything else would reach on_end as a clean stop and hide the failure.
            error = exc
        finally:
            try:
                self._stop_process(proc)
                for pipe in (proc.stdout, proc.stderr):
                    pipe.close()
            finally:
                on_end(error)

    def _pump(self, stdout: Any, on_block: OnBlock) -> None:
        carry = b""
        while not self._closing.is_set():
            data = stdout.read(_READ_BYTES)
            if not data:
                return
            # A pipe read can end mid-frame; frombuffer needs whole frames, so the tail
            # waits for the next read instead of being dropped or delivered misaligned.
            data = carry + data
            whole = len(data) - len(data) % _FRAME_BYTES
            carry = data[whole:]
            if whole:
                block = np.frombuffer(data, dtype="<f4", count=whole // 4)
                on_block(block.reshape(-1, _CHANNELS))

    def _exit_error(self, proc: Any) -> RuntimeError:
        # Reap first: stderr only reaches EOF once the process is gone.
        self._stop_process(proc)
        code = proc.poll()
        stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        return RuntimeError(f"parec for {self.info.name!r} exited unexpectedly "
                            f"(exit code {code}){detail}")


class LinuxBackend:
    """Records the default sink's monitor and the default source through ``parec``.

    Attributes:
        silence_hint: Empty, since monitoring an output needs no OS permission.
    """

    silence_hint = ""

    def __init__(
        self,
        which: Callable[[str], Optional[str]] = shutil.which,
        run: Callable[..., Any] = subprocess.run,
        spawn: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        """Prepare the backend without touching the sound server.

        Args:
            which: Locates an executable on ``PATH``; ``None`` means missing.
            run: ``subprocess.run`` replacement used for ``pactl``.
            spawn: ``subprocess.Popen`` replacement handed to every ``ParecSource``.
        """
        self._which = which
        self._run = run
        self._spawn = spawn

    def open_loopback(self) -> Source:
        """Open the monitor of the default output, which carries the call audio.

        Returns:
            An unstarted source recording what the default sink plays.

        Raises:
            RuntimeError: If ``pactl``/``parec`` are missing, the server cannot be queried,
                or there is no default output.
        """
        return ParecSource(self._loopback_device(), spawn=self._spawn)

    def open_microphone(self) -> Source:
        """Open the default input source.

        Returns:
            An unstarted source recording the microphone.

        Raises:
            RuntimeError: If ``pactl``/``parec`` are missing, the server cannot be queried,
                or the default input is absent or a monitor.
        """
        return ParecSource(self._default_source(), spawn=self._spawn)

    def describe(self) -> str:
        """Return a human-readable listing of the loopback source, microphone and monitors.

        Never raises: each problem is rendered inline as ``UNAVAILABLE (<reason>)``.
        """
        lines = [
            self._describe_line("Call loopback source :", self._loopback_device),
            self._describe_line("Microphone (your voice):", self._default_source),
            "\nAll loopback endpoints:",
        ]
        try:
            lines.extend(f"  - {name}" for name in self._monitor_names())
        except RuntimeError:
            lines.append("  (unavailable)")
        return "\n".join(lines)

    def close(self) -> None:
        """Do nothing: the backend holds no resources, each source owns its ``parec``."""

    @staticmethod
    def _describe_line(label: str, resolve: Callable[[], str]) -> str:
        try:
            return f"{label} {resolve()} @ {SAMPLERATE} Hz"
        except RuntimeError as exc:
            return f"{label} UNAVAILABLE ({exc})"

    def _loopback_device(self) -> str:
        return f"{self._default_sink()}{_MONITOR_SUFFIX}"

    def _default_sink(self) -> str:
        sink, _ = parse_pactl_info(self._pactl("info"))
        if sink is None:
            raise RuntimeError("no default output device: the sound server reports no default sink")
        return sink

    def _default_source(self) -> str:
        _, source = parse_pactl_info(self._pactl("info"))
        # The default source can be a sink's monitor, and recording that as the microphone
        # captures the call a second time, which is exactly the echo this tool exists to avoid.
        if source is None or source.endswith(_MONITOR_SUFFIX):
            raise RuntimeError("no microphone: the sound server has no default input device")
        return source

    def _monitor_names(self) -> List[str]:
        listing = self._pactl("list", "short", "sources")
        names = []
        for line in listing.splitlines():
            fields = line.split("\t")
            if len(fields) > 1 and fields[1].endswith(_MONITOR_SUFFIX):
                names.append(fields[1])
        return names

    def _pactl(self, *args: str) -> str:
        missing = [tool for tool in ("pactl", "parec") if self._which(tool) is None]
        if missing:
            raise RuntimeError(_MISSING_TOOLS.format(" and ".join(missing)))
        argv = ["pactl", *args]
        try:
            # pactl translates its labels per locale, which would hide "Default Sink:" from
            # the parser; LC_ALL=C pins the English text.
            result = self._run(argv, capture_output=True, text=True,
                               env={**os.environ, "LC_ALL": "C"})
        except OSError as exc:
            raise RuntimeError(f"could not run pactl: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"pactl {' '.join(args)} failed ({detail}); is PulseAudio or PipeWire running?")
        return result.stdout
