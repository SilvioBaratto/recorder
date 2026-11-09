"""Tests for backend selection."""

import subprocess
import sys

import pytest

from recorder.backends import select_backend

_IMPORT_CORE = """
import sys
import recorder.cli, recorder.engine, recorder.audio_io, recorder.backends
loaded = {"pyaudiowpatch", "sounddevice", "catap"} & set(sys.modules)
assert not loaded, sorted(loaded)
"""

_IMPORT_CLI = """
import sys
import recorder.cli
assert "numpy" not in sys.modules, "recorder.cli imported numpy"
"""


@pytest.mark.parametrize("platform, backend", [
    ("win32", "WindowsBackend"),
    ("darwin", "MacBackend"),
    ("linux", "LinuxBackend"),
])
def test_select_backend_by_platform(monkeypatch, platform, backend):
    """Each supported platform gets its own backend, built without touching any audio library."""
    monkeypatch.setattr(sys, "platform", platform)
    assert type(select_backend()).__name__ == backend


def test_unsupported_platform_raises(monkeypatch):
    """An unknown platform fails loudly instead of picking a wrong backend."""
    monkeypatch.setattr(sys, "platform", "freebsd13")
    with pytest.raises(RuntimeError, match="freebsd13"):
        select_backend()


def test_core_modules_import_no_audio_library():
    """The CLI and engine load on every OS; only the chosen backend pulls in its audio library."""
    result = subprocess.run([sys.executable, "-c", _IMPORT_CORE], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr


def test_cli_does_not_import_numpy():
    """``status`` and ``stop`` start a fresh process each time; numpy would only slow them down."""
    result = subprocess.run([sys.executable, "-c", _IMPORT_CLI], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
