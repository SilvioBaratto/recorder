"""Tests for the per-device background reader."""

import threading

from fake_pyaudio import LOOPBACK, RATE, FakePyAudio
from recorder.audio_io import DeviceStream


def test_silent_device_releases_its_stream_on_stop():
    """A loopback with nothing playing must not trap its reader inside read() past stop.

    A reader still parked in PortAudio when the daemon tears audio down crashes the process.
    """
    pa = FakePyAudio()
    stop = threading.Event()
    reader = DeviceStream(pa, LOOPBACK, RATE, stop, lambda _msg: None, "loopback")
    device = pa.streams[LOOPBACK["index"]]
    reader.start()
    try:
        assert device.touched.wait(timeout=2)
        stop.set()
        reader.join()
        assert device.closed
        assert not device.reading
    finally:
        pa.release_all()
