"""Platform backends; each platform module is imported lazily so its native deps stay optional."""

from __future__ import annotations

import sys

from .base import Backend, OnBlock, OnEnd, Source, SourceInfo

__all__ = ["Backend", "OnBlock", "OnEnd", "Source", "SourceInfo", "select_backend"]


def select_backend() -> Backend:
    """Create the backend for the current operating system.

    Returns:
        A backend instance for ``sys.platform``.

    Raises:
        RuntimeError: If the platform is not Windows, macOS or Linux.
    """
    platform = sys.platform
    # Imports live inside the branches: pyaudiowpatch, sounddevice and catap are
    # installable on one platform only, so a top-level import would break the others.
    if platform == "win32":
        from .windows import WindowsBackend

        return WindowsBackend()
    if platform == "darwin":
        from .macos import MacBackend

        return MacBackend()
    if platform.startswith("linux"):
        from .linux import LinuxBackend

        return LinuxBackend()
    raise RuntimeError(f"Unsupported platform: {platform!r} (supported: win32, darwin, linux)")
