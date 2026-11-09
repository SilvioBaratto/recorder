"""In-memory stand-ins for ``parec``/``pactl`` so the Linux backend is testable on any OS."""

from __future__ import annotations

import io
import queue
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_PIPE_WAIT_SECONDS = 5.0

SINK = "alsa_output.x.analog-stereo"
SOURCE = "alsa_input.y.analog-stereo"

SOURCES_LISTING = (
    f"0\t{SINK}.monitor\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
    f"1\t{SOURCE}\tPipeWire\ts32le 2ch 48000Hz\tRUNNING\n"
)


def info_text(sink: Optional[str] = SINK, source: Optional[str] = SOURCE) -> str:
    """Build English ``pactl info`` output; ``None`` omits that default's line entirely."""
    lines = ["Server String: /run/user/1000/pulse/native", "Server Name: PulseAudio (on PipeWire 1.0.0)"]
    if sink is not None:
        lines.append(f"Default Sink: {sink}")
    if source is not None:
        lines.append(f"Default Source: {source}")
    lines.append("Cookie: abcd:1234")
    return "\n".join(lines) + "\n"


def which_found(name: str) -> str:
    """Pretend every tool is installed."""
    return f"/usr/bin/{name}"


def which_missing(*absent: str) -> Callable[[str], Optional[str]]:
    """Build a ``which`` that finds everything except ``absent`` (all tools when empty)."""

    def which(name: str) -> Optional[str]:
        """Return a path for an installed tool, ``None`` for a missing one."""
        if absent and name not in absent:
            return f"/usr/bin/{name}"
        return None

    return which


class _Pipe:
    """A blocking byte stream like a subprocess pipe opened with ``bufsize=0``.

    ``read`` returns what is queued (at most ``n`` bytes), blocks while the process is live and
    silent, and returns ``b""`` forever once the process has exited and the queue is drained.
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._pending = b""
        self._eof = False
        self.closed = False

    def feed(self, data: bytes) -> None:
        """Make ``data`` available to the next read."""
        self._queue.put(data)

    def end(self) -> None:
        """Mark EOF behind everything already fed."""
        self._queue.put(None)

    def read(self, n: int = -1) -> bytes:
        """Return up to ``n`` queued bytes, ``b""`` at EOF."""
        if self._pending:
            return self._take(n)
        if self._eof:
            return b""
        try:
            item = self._queue.get(timeout=_PIPE_WAIT_SECONDS)
        except queue.Empty:
            raise TimeoutError("fake parec: nothing fed and the process never exited") from None
        if item is None:
            self._eof = True
            return b""
        self._pending = item
        return self._take(n)

    def _take(self, n: int) -> bytes:
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self) -> None:
        """Record the close so tests can assert no pipe leaks."""
        self.closed = True


class FakeProc:
    """A ``subprocess.Popen`` look-alike that streams scripted stdout and dies on signals.

    Attributes:
        stdout: Pipe the reader consumes.
        stderr: Pipe holding the scripted stderr text.
        returncode: ``None`` while running, then the exit code (-15 after terminate, -9 after kill).
        terminate_calls: Times ``terminate`` was called.
        kill_calls: Times ``kill`` was called.
        wait_calls: The ``timeout`` of every ``wait`` call, in order.
    """

    def __init__(self, chunks: Sequence[bytes] = (), exit_code: Optional[int] = None,
                 stderr: str = "", ignore_terminate: bool = False, ignore_kill: bool = False,
                 args: Optional[List[str]] = None) -> None:
        """Script the process.

        Args:
            chunks: Stdout reads in order; a read returns at most one chunk's remainder.
            exit_code: When set, the process exits by itself with this code right after the
                chunks (an unexpected death). ``None`` keeps it running until signalled.
            stderr: Text parec would have printed.
            ignore_terminate: SIGTERM has no effect, so only ``kill`` ends the process.
            ignore_kill: Nothing ends the process but ``exit``, to model an unkillable one.
            args: argv reported by ``TimeoutExpired``.
        """
        self.args = args or ["parec"]
        self.stdout = _Pipe()
        self.stderr = io.BytesIO(stderr.encode())
        self.returncode: Optional[int] = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: List[Optional[float]] = []
        self._ignore_terminate = ignore_terminate
        self._ignore_kill = ignore_kill
        self._exited = threading.Event()
        self._lock = threading.Lock()
        for chunk in chunks:
            self.stdout.feed(chunk)
        if exit_code is not None:
            self.exit(exit_code)

    def exit(self, code: int) -> None:
        """End the process (first exit wins, like a real one) and let the reader see EOF."""
        with self._lock:
            if self.returncode is not None:
                return
            self.returncode = code
        self._exited.set()
        self.stdout.end()

    def poll(self) -> Optional[int]:
        """Return the exit code, or ``None`` while running."""
        return self.returncode

    def terminate(self) -> None:
        """Deliver SIGTERM."""
        self.terminate_calls += 1
        if not self._ignore_terminate:
            self.exit(-15)

    def kill(self) -> None:
        """Deliver SIGKILL."""
        self.kill_calls += 1
        if not self._ignore_kill:
            self.exit(-9)

    def wait(self, timeout: Optional[float] = None) -> int:
        """Return the exit code, or time out at once (a real wait would sleep) if still running."""
        self.wait_calls.append(timeout)
        if not self._exited.is_set():
            raise subprocess.TimeoutExpired(self.args, timeout)
        assert self.returncode is not None
        return self.returncode


class FakeSpawn:
    """Records every ``Popen`` call and hands back one scripted process.

    Attributes:
        proc: The process every call returns.
        calls: ``(argv, kwargs)`` per call.
    """

    def __init__(self, proc: Optional[FakeProc] = None, error: Optional[OSError] = None) -> None:
        """Script the spawn.

        Args:
            proc: Process to return; a silent, live one by default.
            error: When set, every call raises it, like a binary that cannot be executed.
        """
        self.proc = proc if proc is not None else FakeProc()
        self.error = error
        self.calls: List[Tuple[List[str], Dict[str, Any]]] = []

    def __call__(self, argv: List[str], **kwargs: Any) -> FakeProc:
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return self.proc


class FakeCompleted:
    """The slice of ``subprocess.CompletedProcess`` the backend reads."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class FakeRun:
    """A ``subprocess.run`` stand-in answering the two ``pactl`` commands the backend issues.

    Attributes:
        calls: ``(argv, kwargs)`` per call, so tests can inspect ``env``.
    """

    def __init__(self, info: Optional[FakeCompleted] = None,
                 sources: Optional[FakeCompleted] = None) -> None:
        """Script the two answers.

        Args:
            info: Answer to ``pactl info``; a healthy default sink and source by default.
            sources: Answer to ``pactl list short sources``; one monitor and one microphone.
        """
        self.info = info if info is not None else FakeCompleted(info_text())
        self.sources = sources if sources is not None else FakeCompleted(SOURCES_LISTING)
        self.calls: List[Tuple[List[str], Dict[str, Any]]] = []

    def __call__(self, argv: List[str], **kwargs: Any) -> FakeCompleted:
        self.calls.append((list(argv), kwargs))
        if argv == ["pactl", "info"]:
            return self.info
        if argv == ["pactl", "list", "short", "sources"]:
            return self.sources
        raise AssertionError(f"unexpected command {argv!r}")
