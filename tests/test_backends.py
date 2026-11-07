"""Tests for backend selection."""

import sys

import pytest

from recorder.backends import select_backend


def test_unsupported_platform_raises(monkeypatch):
    """An unknown platform fails loudly instead of picking a wrong backend."""
    monkeypatch.setattr(sys, "platform", "freebsd13")
    with pytest.raises(RuntimeError, match="freebsd13"):
        select_backend()
