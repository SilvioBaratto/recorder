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
