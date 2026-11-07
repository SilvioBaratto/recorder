"""A source that polls a device for available frames instead of blocking in ``read``."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np

from .base import OnBlock, OnEnd, SourceInfo

_JOIN_TIMEOUT_SECONDS = 2.0


def _noop() -> None:
    pass


class PolledSource:
    """Adapts a pull-style device to the push-style ``Source`` protocol.

    It polls ``available`` and only calls ``read`` when frames are waiting. A blocking
    ``read`` is never safe here: a silent loopback never returns from it, and a thread
    parked inside a native read crashes the process at teardown.

    Attributes:
        info: Native format and name of the stream.
    """

    def __init__(
        self,
        info: SourceInfo,
        available: Callable[[], int],
        read: Callable[[int], np.ndarray],
        close_device: Callable[[], None],
        start_device: Callable[[], None] = _noop,
        poll_seconds: float = 0.01,
    ) -> None:
        """Wrap a device without touching it yet.

        Args:
            info: Native format and name of the stream.
            available: Returns how many frames can be read right now without blocking.
                A negative value is a device error code: the stream ends and ``on_end``
                receives an ``OSError`` naming it.
            read: Returns exactly the requested number of frames as a float32 array
                shaped ``(n, channels)``; called only with a positive count that
                ``available`` reported.
            close_device: Releases the device. Invoked at most once.
            start_device: Starts the device; runs on the caller's thread inside ``start``
                so a failure to start propagates to the caller.
            poll_seconds: Pause between polls while no frames are available.
        """
        self.info = info
        self._available = available
        self._read = read
        self._close_device = close_device
        self._start_device = start_device
        self._poll_seconds = poll_seconds
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._release_lock = threading.Lock()
        self._released = False

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Start the device and begin delivering blocks from a daemon thread.

        Args:
            on_block: Called for every captured block, from the polling thread.
            on_end: Called exactly once, from the polling thread, after the device is
                released. Receives ``None`` after ``close`` and the exception if reading
                or ``on_block`` failed.

        Raises:
            Exception: Whatever ``start_device`` or the thread start raises; no thread
                runs then, and ``close`` still releases the device.
        """
        self._start_device()
        thread = threading.Thread(
            target=self._run,
            args=(on_block, on_end),
            name="polled-source-{}".format(self.info.name),
            daemon=True,
        )
        # Published before the thread runs so a close() issued from its first callback
        # sees a live thread instead of releasing the device underneath the loop.
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._thread = None
            raise

    def close(self) -> None:
        """Stop delivery and release the device. Idempotent and never blocks on a read."""
        self._closed.set()
        thread = self._thread
        if thread is None:
            self._release_device()
            return
        # An engine may call close() from on_block/on_end; joining oneself would raise.
        if thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT_SECONDS)

    def _run(self, on_block: OnBlock, on_end: OnEnd) -> None:
        error: Optional[BaseException] = None
        try:
            while not self._closed.is_set():
                n = self._available()
                if n < 0:
                    # PortAudio reports an invalidated device (unplugged, default switched)
                    # as a negative error code; treating it as idle would record silence forever.
                    raise OSError("device reported error {} while polling".format(n))
                if n == 0:
                    self._closed.wait(self._poll_seconds)
                else:
                    on_block(self._read(n))
        except BaseException as exc:
            # Anything else would reach on_end as a clean stop and hide the failure.
            error = exc
        finally:
            try:
                self._release_device()
            finally:
                on_end(error)

    def _release_device(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._close_device()
