"""Interfaces every platform backend implements, so the engine stays platform-agnostic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import numpy as np

OnBlock = Callable[[np.ndarray], None]
"""Receives one float32 block shaped ``(n, channels)`` at the source's native rate."""

OnEnd = Callable[[Optional[BaseException]], None]
"""Receives ``None`` after a normal close, or the exception that stopped delivery."""


@dataclass(frozen=True)
class SourceInfo:
    """Static description of an opened audio source.

    Attributes:
        name: Human-readable device or stream name, used in logs.
        sample_rate: Native sample rate in Hz of the blocks delivered to ``on_block``.
        channels: Channel count of the blocks delivered to ``on_block``.
    """

    name: str
    sample_rate: int
    channels: int


class Source(Protocol):
    """One opened audio stream (loopback or microphone) that pushes blocks to a callback.

    Attributes:
        info: Native format and name of the stream.
    """

    info: SourceInfo

    def start(self, on_block: OnBlock, on_end: OnEnd) -> None:
        """Begin delivering audio from the source's own thread.

        Args:
            on_block: Called for every captured block, from the source's thread.
            on_end: Called exactly once, from the source's thread, when delivery stops.
                Receives ``None`` after a normal ``close`` and the exception otherwise,
                so the engine can tell a clean stop from a device failure.
        """
        ...

    def close(self) -> None:
        """Stop delivery and release the device.

        Idempotent: later calls are no-ops. Never blocks on a device read, so it is
        safe to call from the stop path while the source's thread is mid-read.
        """
        ...


class Backend(Protocol):
    """Factory for the platform's loopback and microphone sources.

    Attributes:
        silence_hint: Advice shown when a capture turns out silent (for example a
            missing OS permission). Empty means the platform has nothing to warn about.
    """

    silence_hint: str

    def open_loopback(self) -> Source:
        """Open the stream that carries the call audio played on this machine.

        Returns:
            A source that has not been started yet.

        Raises:
            RuntimeError: If no loopback capture is available on this system.
        """
        ...

    def open_microphone(self) -> Source:
        """Open the default microphone.

        Returns:
            A source that has not been started yet.

        Raises:
            RuntimeError: If no input device is available.
        """
        ...

    def describe(self) -> str:
        """Return a multi-line, human-readable summary of the devices that would be used."""
        ...

    def close(self) -> None:
        """Release backend-wide resources. Idempotent."""
        ...
