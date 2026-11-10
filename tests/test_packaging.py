"""Tests for the per-platform dependency markers declared in pyproject.toml."""

from importlib.metadata import requires

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _deps_for(platform, py_version):
    """Return the canonical names of the requirements active on a platform."""
    env = {"sys_platform": platform, "python_version": py_version, "extra": ""}
    names = set()
    for line in requires("recorder") or []:
        req = Requirement(line)
        if req.marker is None or req.marker.evaluate(env):
            names.add(canonicalize_name(req.name))
    return names


@pytest.mark.parametrize(
    "platform, py_version, expected",
    [
        ("win32", "3.12", {"numpy", "lameenc", "pyaudiowpatch"}),
        ("linux", "3.12", {"numpy", "lameenc"}),
        ("darwin", "3.12", {"numpy", "lameenc", "sounddevice", "catap"}),
        ("darwin", "3.10", {"numpy", "lameenc", "sounddevice"}),
    ],
)
def test_dependencies_per_platform(platform, py_version, expected):
    """Each platform pulls only its own audio backend; catap needs Python 3.11+."""
    assert _deps_for(platform, py_version) == expected


def test_transcribe_extra_declares_openai_and_miniaudio():
    """The transcribe command's libraries are opt-in and never part of the default install."""
    extra_env = {"sys_platform": "linux", "python_version": "3.12", "extra": "transcribe"}
    default_env = {**extra_env, "extra": ""}
    names = set()
    for line in requires("recorder") or []:
        req = Requirement(line)
        if req.marker and req.marker.evaluate(extra_env) and not req.marker.evaluate(default_env):
            names.add(canonicalize_name(req.name))
    assert names == {"openai", "miniaudio"}
