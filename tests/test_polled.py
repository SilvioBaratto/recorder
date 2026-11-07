"""Tests for the shared polling source."""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

import numpy as np
import pytest

from recorder.backends.base import SourceInfo
from recorder.backends.polled import PolledSource

WAIT = 2.0
INFO = SourceInfo(name="fake", sample_rate=48000, channels=2)


class Probe:
    """Records what a PolledSource does so tests can assert on it without sleeping."""

    def __init__(self) -> None:
        self.blocks: List[np.ndarray] = []
        self.ends: List[Optional[BaseException]] = []
        self.order: List[str] = []
        self.closed_device = 0
        self.polled = threading.Event()
        self.got_block = threading.Event()
        self.ended = threading.Event()

    def on_block(self, block: np.ndarray) -> None:
        """Store the block and wake the test."""
        self.blocks.append(block)
        self.got_block.set()

    def on_end(self, error: Optional[BaseException]) -> None:
        """Store the end reason and wake the test."""
        self.ends.append(error)
        self.ended.set()

    def close_device(self) -> None:
        """Count device releases so double-close shows up."""
        self.closed_device += 1


def _zeros(n: int) -> np.ndarray:
    return np.zeros((n, 2), dtype=np.float32)


def _one_burst() -> Callable[[], int]:
    pending = [4]
    return lambda: pending.pop() if pending else 0


def test_delivers_blocks_until_closed():
    """A burst is delivered, then close ends the stream cleanly exactly once."""
    probe = Probe()
    src = PolledSource(INFO, _one_burst(), _zeros, probe.close_device)
    src.start(probe.on_block, probe.on_end)
    assert probe.got_block.wait(WAIT)

    src.close()
    src.close()

    assert len(probe.blocks) >= 1
    assert probe.blocks[0].shape == (4, 2)
    assert probe.ends == [None]
    assert probe.closed_device == 1


def test_never_reads_when_nothing_is_available():
    """A silent loopback must not park the thread in read(), or close would hang."""
    probe = Probe()

    def _available() -> int:
        probe.polled.set()
        return 0

    def _read(n: int) -> np.ndarray:
        raise AssertionError("read() must not be called when nothing is available")

    src = PolledSource(INFO, _available, _read, probe.close_device)
    src.start(probe.on_block, probe.on_end)
    assert probe.polled.wait(WAIT)

    began = time.monotonic()
    src.close()

    assert time.monotonic() - began < 1.0
    assert probe.ends == [None]


def test_read_error_is_reported_through_on_end():
    """A failing device surfaces as the on_end error and still releases the device."""
    probe = Probe()
    boom = RuntimeError("boom")

    def _read(n: int) -> np.ndarray:
        raise boom

    src = PolledSource(INFO, lambda: 4, _read, probe.close_device)
    src.start(probe.on_block, probe.on_end)
    assert probe.ended.wait(WAIT)

    src.close()

    assert probe.ends == [boom]
    assert probe.closed_device == 1


def test_close_is_idempotent_and_safe_before_start():
    """Closing a never-started source releases the device once and touches nothing else."""

    def _unexpected(*_args) -> int:
        raise AssertionError("a source that was never started must not touch the device")

    probe = Probe()
    src = PolledSource(INFO, _unexpected, _unexpected, probe.close_device, start_device=_unexpected)

    src.close()
    src.close()

    assert probe.closed_device == 1
    assert probe.ends == []


def test_start_device_runs_before_first_read():
    """The device must be started before any read, or the first read would fail."""
    probe = Probe()

    def _read(n: int) -> np.ndarray:
        probe.order.append("read")
        return _zeros(n)

    src = PolledSource(
        INFO,
        _one_burst(),
        _read,
        probe.close_device,
        start_device=lambda: probe.order.append("start_device"),
    )
    src.start(probe.on_block, probe.on_end)
    assert probe.got_block.wait(WAIT)
    src.close()

    assert probe.order[:2] == ["start_device", "read"]


def test_close_from_the_source_thread_does_not_deadlock():
    """An engine may call close() from a callback; joining the current thread would raise."""
    probe = Probe()
    holder: List[PolledSource] = []

    def _on_block(block: np.ndarray) -> None:
        holder[0].close()
        probe.on_block(block)

    def _on_end(error: Optional[BaseException]) -> None:
        holder[0].close()
        probe.on_end(error)

    holder.append(PolledSource(INFO, lambda: 4, _zeros, probe.close_device))
    holder[0].start(_on_block, _on_end)
    assert probe.ended.wait(WAIT)
    holder[0].close()

    assert probe.ends == [None]
    assert probe.closed_device == 1


def test_failed_thread_start_still_lets_close_release_the_device(monkeypatch):
    """If the OS refuses a thread, close() must release the device instead of joining it."""

    def _refuse(self) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)
    probe = Probe()
    src = PolledSource(INFO, lambda: 0, _zeros, probe.close_device)

    with pytest.raises(RuntimeError, match="new thread"):
        src.start(probe.on_block, probe.on_end)
    src.close()

    assert probe.closed_device == 1
    assert probe.ends == []


def test_negative_available_is_reported_as_an_error():
    """PortAudio signals a lost device with a negative count, which must not look like idle."""
    probe = Probe()

    def _read(n: int) -> np.ndarray:
        raise AssertionError("read() must not be called after a failed poll")

    src = PolledSource(INFO, lambda: -9983, _read, probe.close_device)
    src.start(probe.on_block, probe.on_end)
    assert probe.ended.wait(WAIT)

    src.close()

    assert len(probe.ends) == 1
    assert isinstance(probe.ends[0], OSError)
    assert "-9983" in str(probe.ends[0])
    assert probe.closed_device == 1
