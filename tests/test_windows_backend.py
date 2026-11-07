"""Tests for the PyAudioWPatch backend, run against in-memory PortAudio fakes on any OS."""

from __future__ import annotations

import threading
from typing import Dict, List

from fake_pyaudio import LOOPBACK, MIC, RATE, FakePyAudio, FakePyAudioModule, FakeWindowsBackend
from recorder.backends.base import SourceInfo
from recorder.backends.windows import WindowsBackend

WAIT = 2.0

SPEAKERS = {"index": 0, "name": "Speakers (Realtek)", "defaultSampleRate": 48000.0,
            "maxInputChannels": 0}
HEADSET = {"index": 3, "name": "Headset Microphone", "defaultSampleRate": 16000.0,
           "maxInputChannels": 1}
SPEAKERS_LOOPBACK = {"index": 7, "name": "Speakers (Realtek) [Loopback]",
                     "defaultSampleRate": 48000.0, "maxInputChannels": 2,
                     "isLoopbackDevice": True}
HDMI_LOOPBACK = {"index": 8, "name": "HDMI Output [Loopback]", "defaultSampleRate": 48000.0,
                 "maxInputChannels": 2, "isLoopbackDevice": True}


class _WasapiTable:
    """Answers PortAudio's WASAPI host-API queries from a fixed device table."""

    def __init__(self, default_output: dict, loopbacks: List[dict]) -> None:
        self.devices: Dict[int, dict] = {d["index"]: d for d in [default_output, HEADSET, *loopbacks]}
        self.default_output = default_output
        self.loopbacks = loopbacks

    def get_host_api_info_by_type(self, host_api: int) -> dict:
        """Return the WASAPI host API's defaults; any other host API is a test failure."""
        assert host_api == FakePyAudioModule.paWASAPI
        return {"defaultOutputDevice": self.default_output["index"],
                "defaultInputDevice": HEADSET["index"]}

    def get_device_info_by_index(self, index: int) -> dict:
        """Return the device registered at ``index``."""
        return self.devices[index]

    def get_loopback_device_info_generator(self):
        """Yield every loopback endpoint."""
        return iter(self.loopbacks)


def test_describe_keeps_the_existing_format():
    """``recorder devices`` on Windows prints exactly what it printed before backends existed."""
    backend = FakeWindowsBackend(FakePyAudioModule(FakePyAudio()))
    assert backend.describe() == (
        "Call loopback source : Fake Speakers [Loopback] @ 48000 Hz\n"
        "Microphone (your voice): Fake Microphone @ 48000 Hz\n"
        "\n"
        "All loopback endpoints:\n"
        "  - Fake Speakers [Loopback]"
    )


def test_loopback_is_the_endpoint_named_after_the_default_output():
    """The default speakers are matched to their loopback twin; the mic is the default input."""
    backend = WindowsBackend(FakePyAudioModule(_WasapiTable(SPEAKERS, [HDMI_LOOPBACK,
                                                                         SPEAKERS_LOOPBACK])))
    assert backend.find_loopback_device() is SPEAKERS_LOOPBACK
    assert backend.find_input_device() is HEADSET


def test_default_output_that_is_already_a_loopback_is_used_directly():
    """An output reported as a loopback device needs no name matching."""
    backend = WindowsBackend(FakePyAudioModule(_WasapiTable(SPEAKERS_LOOPBACK, [])))
    assert backend.find_loopback_device() is SPEAKERS_LOOPBACK


def test_describe_reports_a_missing_loopback_instead_of_failing():
    """With no matching endpoint the listing still prints, naming the default output."""
    backend = WindowsBackend(FakePyAudioModule(_WasapiTable(SPEAKERS, [HDMI_LOOPBACK])))
    assert backend.describe() == (
        "Call loopback source : UNAVAILABLE "
        "(no loopback endpoint found for default output 'Speakers (Realtek)')\n"
        "Microphone (your voice): Headset Microphone @ 16000 Hz\n"
        "\n"
        "All loopback endpoints:\n"
        "  - HDMI Output [Loopback]"
    )


def test_streams_open_with_the_device_native_format():
    """Each device opens as float32 input at its own rate and channel count, 0.1 s buffers."""
    pa = FakePyAudio()
    backend = FakeWindowsBackend(FakePyAudioModule(pa))
    loopback = backend.open_loopback()
    microphone = backend.open_microphone()
    try:
        assert loopback.info == SourceInfo(name=LOOPBACK["name"], sample_rate=RATE, channels=2)
        assert microphone.info == SourceInfo(name=MIC["name"], sample_rate=RATE, channels=1)
        assert pa.open_kwargs[LOOPBACK["index"]] == {
            "format": FakePyAudioModule.paFloat32, "channels": 2, "rate": RATE,
            "frames_per_buffer": RATE // 10, "input": True,
            "input_device_index": LOOPBACK["index"],
        }
        assert pa.open_kwargs[MIC["index"]]["channels"] == 1
    finally:
        loopback.close()
        microphone.close()
        backend.close()


def test_blocks_arrive_as_float32_frames_and_close_releases_the_device():
    """Raw bytes become ``(frames, channels)`` float32, and closing stops then closes the stream."""
    pa = FakePyAudio()
    backend = FakeWindowsBackend(FakePyAudioModule(pa))
    microphone = backend.open_microphone()
    blocks = []
    got_block = threading.Event()

    def on_block(block):
        """Keep the block and wake the test."""
        blocks.append(block)
        got_block.set()

    microphone.start(on_block, lambda _error: None)
    try:
        assert got_block.wait(WAIT)
    finally:
        microphone.close()
        backend.close()
    assert blocks[0].dtype == "float32"
    assert blocks[0].shape == (RATE // 100, 1)
    assert pa.streams[MIC["index"]].closed


def test_close_terminates_portaudio_once():
    """Closing twice is harmless, and a backend that never opened anything starts no PortAudio."""
    pa = FakePyAudio()
    backend = FakeWindowsBackend(FakePyAudioModule(pa))
    backend.close()
    assert pa.terminate_calls == 0
    backend.open_loopback().close()
    backend.close()
    backend.close()
    assert pa.terminate_calls == 1
