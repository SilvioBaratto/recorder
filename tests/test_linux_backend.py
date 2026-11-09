"""Tests for the PulseAudio/PipeWire backend, run against in-memory parec/pactl fakes on any OS."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Iterator, List, Optional

import numpy as np
import pytest

from fake_parec import (
    SINK,
    SOURCE,
    FakeCompleted,
    FakeProc,
    FakeRun,
    FakeSpawn,
    info_text,
    which_found,
    which_missing,
)
from recorder.backends import linux, select_backend
from recorder.backends.base import SourceInfo
from recorder.backends.linux import LinuxBackend, ParecSource, parse_pactl_info
from recorder.config import BLOCK

WAIT = 2.0

PAREC_ARGV = ["parec", "--device=sink.monitor", "--format=float32le", "--rate=48000",
              "--channels=2", "--latency-msec=100"]

_IMPORT_LINUX = """
import sys
import recorder.backends.linux
loaded = {"pyaudiowpatch", "sounddevice", "catap", "soundcard", "pyaudio"} & set(sys.modules)
assert not loaded, sorted(loaded)
"""


class Probe:
    """Records what a source delivers so tests can wait on events instead of sleeping."""

    def __init__(self) -> None:
        self.blocks: List[np.ndarray] = []
        self.ends: List[Optional[BaseException]] = []
        self.frames = 0
        self.ended = threading.Event()
        self.got_frames = threading.Event()
        self.want_frames = 1

    def on_block(self, block: np.ndarray) -> None:
        """Store the block and wake the test once enough frames arrived."""
        self.blocks.append(block)
        self.frames += len(block)
        if self.frames >= self.want_frames:
            self.got_frames.set()

    def on_end(self, error: Optional[BaseException]) -> None:
        """Store the end reason and wake the test."""
        self.ends.append(error)
        self.ended.set()


@pytest.fixture(autouse=True)
def no_leaked_reader_threads() -> Iterator[None]:
    """Fail a test that leaves a reader thread behind, since it would outlive the suite."""
    yield
    leaked = [t for t in threading.enumerate() if t.name.startswith("parec-")]
    for thread in leaked:
        thread.join(WAIT)
    assert not [t for t in leaked if t.is_alive()]


def _frames(*values: float) -> bytes:
    return np.array(values, dtype="<f4").tobytes()


def _started(proc: FakeProc, probe: Probe, device: str = "sink.monitor"):
    spawn = FakeSpawn(proc)
    source = ParecSource(device, spawn=spawn)
    source.start(probe.on_block, probe.on_end)
    return source, spawn


def _backend(run: Optional[FakeRun] = None, spawn: Optional[FakeSpawn] = None,
             which=which_found) -> LinuxBackend:
    return LinuxBackend(which=which, run=run or FakeRun(), spawn=spawn or FakeSpawn())


SAMPLE_INFO = """\
Server String: /run/user/1000/pulse/native
Server Name: PulseAudio (on PipeWire 1.0.0)
Default Sample Specification: float32le 2ch 48000Hz
Default Sink: alsa_output.x.analog-stereo
Default Source: alsa_input.y.analog-stereo
Cookie: abcd:1234
"""


# Captured from PulseAudio 16.1 (ubuntu:24.04, scripts/e2e_linux.sh) with no sink and no source
# loaded: a missing default is printed as "(null)".
REAL_EMPTY_INFO = """\
Server String: /tmp/runtime-tester/pulse/native
Library Protocol Version: 35
Server Protocol Version: 35
Is Local: yes
Client Index: 1
Tile Size: 65472
User Name: tester
Host Name: 83b102239f3a
Server Name: pulseaudio
Server Version: 16.1
Default Sample Specification: s16le 2ch 44100Hz
Default Channel Map: front-left,front-right
Default Sink: (null)
Default Source: (null)
Cookie: 14c6:dc7a
"""


def _empty_server() -> FakeRun:
    return FakeRun(info=FakeCompleted(REAL_EMPTY_INFO), sources=FakeCompleted(""))


def test_parse_pactl_info_extracts_defaults():
    """The two ``Default ...:`` lines are the only ones read, whatever else the server prints."""
    assert parse_pactl_info(SAMPLE_INFO) == (SINK, SOURCE)


@pytest.mark.parametrize("text", [
    "Default Sink: n/a\nDefault Source: n/a\n",
    "Default Sink:\nDefault Source:   \n",
    "Server Name: PipeWire\n",
    REAL_EMPTY_INFO,
    "Default Sink: @DEFAULT_SINK@\nDefault Source: @DEFAULT_SOURCE@\n",
])
def test_parse_pactl_info_reports_unusable_defaults_as_none(text):
    """A server with no default device must not look like a device literally named ``n/a``."""
    assert parse_pactl_info(text) == (None, None)


def test_real_empty_server_has_no_default_output():
    """PulseAudio's ``(null)`` placeholder is reported as a missing output, not recorded."""
    with pytest.raises(RuntimeError, match="no default output"):
        _backend(run=_empty_server()).open_loopback()


def test_real_empty_server_has_no_microphone():
    """The same placeholder for the source means there is no microphone to record."""
    with pytest.raises(RuntimeError, match="no microphone"):
        _backend(run=_empty_server()).open_microphone()


def test_describe_on_a_real_empty_server_lists_no_placeholder_device():
    """``recorder devices`` explains the missing defaults instead of naming ``(null).monitor``."""
    text = _backend(run=_empty_server()).describe()
    lines = text.splitlines()
    assert lines[0].startswith("Call loopback source : UNAVAILABLE (no default output")
    assert lines[1].startswith("Microphone (your voice): UNAVAILABLE (no microphone")
    assert "(null)" not in text


def test_pactl_output_is_decoded_leniently():
    """A byte the locale cannot decode must not crash ``recorder devices`` or ``start``."""
    run = FakeRun()
    _backend(run=run).describe()
    assert run.calls
    assert all(kwargs.get("errors") == "replace" for _, kwargs in run.calls)


def test_pactl_runs_with_c_locale(monkeypatch):
    """Labels are translated under other locales, which would hide the defaults from the parser."""
    monkeypatch.setenv("LC_ALL", "it_IT.UTF-8")
    run = FakeRun()
    _backend(run=run).open_loopback()
    argv, kwargs = run.calls[0]
    assert argv == ["pactl", "info"]
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["PATH"] == os.environ["PATH"]
    assert kwargs["capture_output"] is True and kwargs["text"] is True


def test_every_pactl_call_in_describe_runs_with_c_locale(monkeypatch):
    """The listing is parsed too, so it needs the same untranslated environment."""
    monkeypatch.setenv("LC_ALL", "it_IT.UTF-8")
    run = FakeRun()
    _backend(run=run).describe()
    assert len(run.calls) >= 3
    assert all(kwargs["env"]["LC_ALL"] == "C" for _, kwargs in run.calls)


def test_frames_are_reassembled_across_split_reads():
    """A frame split over pipe reads is delivered whole, in order, never as a partial block."""
    samples = _frames(0.1, -0.1, 0.2, -0.2, 0.3, -0.3)
    proc = FakeProc(chunks=[samples[:5], samples[5:16], samples[16:]])
    probe = Probe()
    probe.want_frames = 3
    source, _ = _started(proc, probe)
    assert probe.got_frames.wait(WAIT)
    source.close()
    assert [len(b) for b in probe.blocks] == [2, 1]
    got = np.concatenate(probe.blocks)
    assert got.dtype == np.float32 and got.shape == (3, 2)
    np.testing.assert_array_equal(got, np.frombuffer(samples, dtype="<f4").reshape(-1, 2))


def _with_reserve(proc: FakeProc, reserve: int) -> ParecSource:
    return ParecSource("sink.monitor", spawn=FakeSpawn(proc), reserve_frames=reserve)


def test_reserve_holds_frames_back_then_streams():
    """Nothing reaches the consumer until the reserve arrived; it goes out at once, then each read."""
    proc = FakeProc()
    probe = Probe()
    source = _with_reserve(proc, reserve=4)
    source.start(probe.on_block, probe.on_end)
    try:
        proc.stdout.feed(_frames(0.1, -0.1, 0.2, -0.2, 0.3, -0.3))
        assert proc.stdout.wait_handled(WAIT)
        assert probe.blocks == []
        proc.stdout.feed(_frames(0.4, -0.4, 0.5, -0.5))
        assert proc.stdout.wait_handled(WAIT)
        assert [len(b) for b in probe.blocks] == [5]
        proc.stdout.feed(_frames(0.6, -0.6))
        assert proc.stdout.wait_handled(WAIT)
        assert [len(b) for b in probe.blocks] == [5, 1]
    finally:
        source.close()
    assert probe.ended.wait(WAIT)
    assert probe.ends == [None]
    expected = np.array([0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4, -0.4, 0.5, -0.5, 0.6, -0.6],
                        dtype=np.float32).reshape(-1, 2)
    np.testing.assert_array_equal(np.concatenate(probe.blocks), expected)


def test_reserve_keeps_frames_whole_across_split_reads():
    """Held-back audio still goes through the partial-frame carry, so no frame is torn."""
    samples = _frames(0.1, -0.1, 0.2, -0.2, 0.3, -0.3)
    proc = FakeProc(chunks=[samples[:5], samples[5:16], samples[16:]])
    probe = Probe()
    probe.want_frames = 3
    source = _with_reserve(proc, reserve=3)
    source.start(probe.on_block, probe.on_end)
    assert probe.got_frames.wait(WAIT)
    source.close()
    assert [len(b) for b in probe.blocks] == [3]
    np.testing.assert_array_equal(probe.blocks[0], np.frombuffer(samples, dtype="<f4").reshape(-1, 2))


def test_reserve_not_reached_is_dropped_on_close():
    """Audio still held back when the recording stops is discarded, and the end is clean."""
    proc = FakeProc()
    probe = Probe()
    source = _with_reserve(proc, reserve=10)
    source.start(probe.on_block, probe.on_end)
    proc.stdout.feed(_frames(0.5, 0.5, 0.25, 0.25))
    assert proc.stdout.wait_handled(WAIT)
    source.close()
    assert probe.ended.wait(WAIT)
    assert probe.blocks == []
    assert probe.ends == [None]


def test_reserve_not_reached_is_delivered_before_an_unexpected_exit():
    """A parec that dies before the reserve fills still hands over its audio, then the error."""
    proc = FakeProc(chunks=[_frames(0.5, 0.5, 0.25, 0.25)], exit_code=1, stderr="Stream error\n")
    events: List[tuple] = []
    ended = threading.Event()

    def on_block(block: np.ndarray) -> None:
        """Record the block in arrival order."""
        events.append(("block", block.copy()))

    def on_end(error: Optional[BaseException]) -> None:
        """Record the end after any block."""
        events.append(("end", error))
        ended.set()

    source = _with_reserve(proc, reserve=10)
    source.start(on_block, on_end)
    assert ended.wait(WAIT)
    source.close()
    assert [kind for kind, _ in events] == ["block", "end"]
    np.testing.assert_array_equal(events[0][1], np.array([[0.5, 0.5], [0.25, 0.25]], dtype=np.float32))
    error = events[1][1]
    assert isinstance(error, RuntimeError)
    assert "exit code 1" in str(error) and "Stream error" in str(error)


def test_loopback_is_held_back_by_its_reserve():
    """The call side reaches the mixer only once its reserve of whole blocks arrived, all at once."""
    reserve_blocks, remainder = divmod(linux._LOOPBACK_RESERVE_FRAMES, BLOCK)
    assert reserve_blocks >= 1 and remainder == 0
    proc = FakeProc()
    probe = Probe()
    source = _backend(spawn=FakeSpawn(proc)).open_loopback()
    source.start(probe.on_block, probe.on_end)
    block = np.full((BLOCK, 2), 0.25, dtype="<f4").tobytes()
    try:
        for _ in range(reserve_blocks - 1):
            proc.stdout.feed(block)
            assert proc.stdout.wait_handled(WAIT)
            assert probe.frames == 0
        proc.stdout.feed(block)
        assert proc.stdout.wait_handled(WAIT)
        assert [len(b) for b in probe.blocks] == [linux._LOOPBACK_RESERVE_FRAMES]
    finally:
        source.close()
    assert probe.ended.wait(WAIT)


def test_microphone_is_never_held_back():
    """The microphone clocks the mixer, so each of its reads is delivered as it arrives."""
    proc = FakeProc()
    probe = Probe()
    source = _backend(spawn=FakeSpawn(proc)).open_microphone()
    source.start(probe.on_block, probe.on_end)
    block = np.full((BLOCK, 2), 0.25, dtype="<f4").tobytes()
    try:
        proc.stdout.feed(block)
        assert proc.stdout.wait_handled(WAIT)
        assert [len(b) for b in probe.blocks] == [BLOCK]
    finally:
        source.close()
    assert probe.ended.wait(WAIT)


def test_info_describes_the_native_format():
    """The engine resamples from ``info``, so it must state what parec was asked to produce."""
    source = ParecSource("sink.monitor", spawn=FakeSpawn())
    assert source.info == SourceInfo(name="sink.monitor", sample_rate=48000, channels=2)


def test_parec_command_line():
    """parec converts and resamples itself, so the flags are the whole format contract."""
    probe = Probe()
    source, spawn = _started(FakeProc(), probe)
    source.close()
    argv, kwargs = spawn.calls[0]
    assert argv == PAREC_ARGV
    assert kwargs["bufsize"] == 0
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE


def test_close_terminates_then_ends_cleanly():
    """A requested stop ends with ``None``, signals once and a second close does nothing."""
    proc = FakeProc()
    probe = Probe()
    source, _ = _started(proc, probe)
    source.close()
    assert probe.ended.wait(WAIT)
    source.close()
    assert probe.ends == [None]
    assert proc.terminate_calls == 1 and proc.kill_calls == 0
    assert proc.wait_calls == [1]
    assert proc.stdout.closed and proc.stderr.closed


def test_close_escalates_to_kill():
    """A parec that ignores SIGTERM is killed instead of stalling the stop path."""
    proc = FakeProc(ignore_terminate=True)
    probe = Probe()
    source, _ = _started(proc, probe)
    source.close()
    assert probe.ended.wait(WAIT)
    assert probe.ends == [None]
    assert proc.terminate_calls == 1 and proc.kill_calls == 1
    assert proc.returncode == -9


def test_close_returns_even_if_the_process_cannot_be_killed(monkeypatch):
    """An unkillable process (uninterruptible sleep) must not hang the caller of ``close``."""
    monkeypatch.setattr(linux, "_JOIN_SECONDS", 0.05)
    proc = FakeProc(ignore_terminate=True, ignore_kill=True)
    probe = Probe()
    source, _ = _started(proc, probe)
    try:
        source.close()
        assert proc.kill_calls == 1
        assert not probe.ended.is_set()
    finally:
        proc.exit(-9)
    assert probe.ended.wait(WAIT)


def test_unexpected_exit_is_reported():
    """parec dying by itself surfaces its exit code and stderr instead of looking like silence."""
    proc = FakeProc(chunks=[_frames(0.5, 0.5)], exit_code=3, stderr="Connection refused\n")
    probe = Probe()
    source, _ = _started(proc, probe)
    assert probe.ended.wait(WAIT)
    source.close()
    assert len(probe.ends) == 1
    error = probe.ends[0]
    assert isinstance(error, RuntimeError)
    assert "exit code 3" in str(error)
    assert "Connection refused" in str(error)
    assert sum(len(b) for b in probe.blocks) == 1


def test_on_block_failure_is_reported_and_stops_parec():
    """An exception from the consumer ends the stream with that exception and frees the process."""
    proc = FakeProc(chunks=[_frames(0.5, 0.5)])
    probe = Probe()
    boom = ValueError("consumer broke")

    def on_block(block: np.ndarray) -> None:
        """Fail like a consumer that cannot keep the block."""
        raise boom

    source = ParecSource("sink.monitor", spawn=FakeSpawn(proc))
    source.start(on_block, probe.on_end)
    assert probe.ended.wait(WAIT)
    source.close()
    assert probe.ends == [boom]
    assert proc.terminate_calls == 1
    assert proc.returncode == -15


def test_close_from_on_block_does_not_join_itself():
    """The engine may stop a source from inside its own callback; that must not self-join."""
    proc = FakeProc(chunks=[_frames(0.5, 0.5)])
    probe = Probe()
    holder: List[ParecSource] = []

    def on_block(block: np.ndarray) -> None:
        """Stop the source from its own reader thread."""
        holder[0].close()

    source = ParecSource("sink.monitor", spawn=FakeSpawn(proc))
    holder.append(source)
    source.start(on_block, probe.on_end)
    assert probe.ended.wait(WAIT)
    source.close()
    assert probe.ends == [None]
    assert proc.terminate_calls == 1


def test_close_before_start_spawns_nothing_and_never_ends():
    """Opening then discarding a source must not leave a recorder process or a stray callback."""
    spawn = FakeSpawn()
    source = ParecSource("sink.monitor", spawn=spawn)
    source.close()
    source.close()
    assert spawn.calls == []


def test_start_after_close_raises():
    """A closed source never revives, so a late start cannot leak a parec process."""
    spawn = FakeSpawn()
    source = ParecSource("sink.monitor", spawn=spawn)
    source.close()
    probe = Probe()
    with pytest.raises(RuntimeError, match="closed"):
        source.start(probe.on_block, probe.on_end)
    assert spawn.calls == []
    assert not probe.ended.is_set()


def test_start_twice_raises():
    """A second start would orphan the first process."""
    probe = Probe()
    source, spawn = _started(FakeProc(), probe)
    try:
        with pytest.raises(RuntimeError, match="already started"):
            source.start(probe.on_block, probe.on_end)
        assert len(spawn.calls) == 1
    finally:
        source.close()
    assert probe.ended.wait(WAIT)


def test_spawn_failure_is_a_runtime_error():
    """An unrunnable parec reaches the user as an actionable error, not a bare OSError."""
    spawn = FakeSpawn(error=FileNotFoundError(2, "No such file or directory"))
    source = ParecSource("sink.monitor", spawn=spawn)
    probe = Probe()
    with pytest.raises(RuntimeError, match="parec"):
        source.start(probe.on_block, probe.on_end)
    assert not probe.ended.is_set()


def test_open_loopback_records_the_default_sink_monitor():
    """The call audio is whatever plays on the default output, i.e. that sink's monitor."""
    spawn = FakeSpawn()
    source = _backend(spawn=spawn).open_loopback()
    assert source.info == SourceInfo(name=f"{SINK}.monitor", sample_rate=48000, channels=2)
    assert spawn.calls == []


def test_open_microphone_records_the_default_source():
    """The microphone is the server's default input."""
    spawn = FakeSpawn()
    source = _backend(spawn=spawn).open_microphone()
    assert source.info == SourceInfo(name=SOURCE, sample_rate=48000, channels=2)
    assert spawn.calls == []


def test_opened_source_spawns_parec_for_its_device():
    """The backend hands its spawn function to the sources it creates."""
    spawn = FakeSpawn()
    source = _backend(spawn=spawn).open_microphone()
    probe = Probe()
    source.start(probe.on_block, probe.on_end)
    source.close()
    assert probe.ended.wait(WAIT)
    assert spawn.calls[0][0][:2] == ["parec", f"--device={SOURCE}"]


@pytest.mark.parametrize("absent", [("pactl",), ("parec",), ()])
@pytest.mark.parametrize("opener", ["open_loopback", "open_microphone"])
def test_missing_tools_name_the_package(opener, absent):
    """Both tools ship in one package, so the error names it, including Arch's name."""
    backend = _backend(which=which_missing(*absent))
    with pytest.raises(RuntimeError) as excinfo:
        getattr(backend, opener)()
    assert "pulseaudio-utils" in str(excinfo.value)
    assert "libpulse" in str(excinfo.value)


def test_missing_tool_error_names_what_is_missing():
    """Naming the absent binary tells the user which half of the install failed."""
    with pytest.raises(RuntimeError) as excinfo:
        _backend(which=which_missing("parec")).open_loopback()
    assert "parec" in str(excinfo.value)
    assert "pactl" not in str(excinfo.value)


def test_no_default_sink_is_rejected():
    """Without an output there is no monitor to record the call from."""
    run = FakeRun(info=FakeCompleted(info_text(sink=None)))
    with pytest.raises(RuntimeError, match="no default output"):
        _backend(run=run).open_loopback()


def test_default_source_monitor_is_rejected():
    """A monitor as microphone would record the call a second time, as an echo."""
    run = FakeRun(info=FakeCompleted(info_text(source="alsa_output.x.monitor")))
    with pytest.raises(RuntimeError, match="no microphone"):
        _backend(run=run).open_microphone()


@pytest.mark.parametrize("source", [None, "n/a"])
def test_missing_default_source_is_rejected(source):
    """A machine without an input device fails clearly rather than recording nothing."""
    run = FakeRun(info=FakeCompleted(info_text(source=source)))
    with pytest.raises(RuntimeError, match="no microphone"):
        _backend(run=run).open_microphone()


def test_no_default_sink_message_names_the_remedy():
    """The error says which command selects an output and which one lists them."""
    run = FakeRun(info=FakeCompleted(info_text(sink=None)))
    with pytest.raises(RuntimeError) as excinfo:
        _backend(run=run).open_loopback()
    message = str(excinfo.value)
    assert "no default output" in message
    assert "pactl set-default-sink" in message
    assert "pactl list short sinks" in message


def test_monitor_as_microphone_message_names_the_source_and_the_remedy():
    """The refusal names the offending monitor, so the user knows what to change and how."""
    monitor = f"{SINK}.monitor"
    run = FakeRun(info=FakeCompleted(info_text(source=monitor)))
    with pytest.raises(RuntimeError) as excinfo:
        _backend(run=run).open_microphone()
    message = str(excinfo.value)
    assert "no microphone" in message
    assert f"'{monitor}'" in message
    assert "monitor of an output" in message
    assert "pactl set-default-source" in message
    assert "pactl list short sources" in message


def test_missing_microphone_message_names_the_remedy():
    """With no default input at all, the error still says how to choose one."""
    run = FakeRun(info=FakeCompleted(info_text(source=None)))
    with pytest.raises(RuntimeError) as excinfo:
        _backend(run=run).open_microphone()
    message = str(excinfo.value)
    assert "no microphone" in message
    assert "pactl set-default-source" in message
    assert "pactl list short sources" in message


def test_pactl_failure_is_a_runtime_error_with_its_stderr():
    """A stopped sound server shows pactl's own complaint."""
    run = FakeRun(info=FakeCompleted("", returncode=1, stderr="Connection failure: Refused"))
    with pytest.raises(RuntimeError, match="Connection failure: Refused"):
        _backend(run=run).open_loopback()


def test_pactl_that_cannot_run_is_a_runtime_error():
    """``which`` can find a binary that still fails to execute."""

    def run(argv, **kwargs):
        """Fail like a binary whose execute bit is missing."""
        raise PermissionError(13, "Permission denied")

    backend = LinuxBackend(which=which_found, run=run, spawn=FakeSpawn())
    with pytest.raises(RuntimeError, match="pactl"):
        backend.open_loopback()


def test_describe_lists_devices_in_the_windows_shape():
    """Same three sections as the Windows backend, with only monitors under the endpoint list."""
    assert _backend().describe() == (
        f"Call loopback source : {SINK}.monitor @ 48000 Hz\n"
        f"Microphone (your voice): {SOURCE} @ 48000 Hz\n"
        "\n"
        "All loopback endpoints:\n"
        f"  - {SINK}.monitor"
    )


def test_describe_reports_problems_instead_of_raising():
    """``recorder devices`` is the diagnostic, so it must explain a broken setup, not crash on it."""
    text = _backend(which=which_missing()).describe()
    assert text.count("UNAVAILABLE") == 2
    assert "pulseaudio-utils" in text
    assert "(unavailable)" in text


def test_describe_keeps_the_other_sections_when_one_fails():
    """A missing sink must not hide a working microphone."""
    run = FakeRun(info=FakeCompleted(info_text(sink=None)))
    lines = _backend(run=run).describe().splitlines()
    assert lines[0].startswith("Call loopback source : UNAVAILABLE (")
    assert "no default output" in lines[0]
    assert lines[1] == f"Microphone (your voice): {SOURCE} @ 48000 Hz"
    assert lines[-1] == f"  - {SINK}.monitor"


def test_describe_reports_a_bad_microphone_inline():
    """A monitor as default source is shown as unavailable, not as a usable microphone."""
    run = FakeRun(info=FakeCompleted(info_text(source=f"{SINK}.monitor")))
    lines = _backend(run=run).describe().splitlines()
    assert lines[1].startswith("Microphone (your voice): UNAVAILABLE (")
    assert "no microphone" in lines[1]


def test_describe_survives_a_failing_sources_listing():
    """The endpoint list is auxiliary; its failure leaves the heading with a marker."""
    run = FakeRun(sources=FakeCompleted("", returncode=1, stderr="boom"))
    text = _backend(run=run).describe()
    assert text.endswith("All loopback endpoints:\n  (unavailable)")
    assert "Call loopback source : " + SINK + ".monitor" in text


def test_backend_has_nothing_to_warn_about_or_release():
    """Loopback on Linux needs no OS permission and the backend holds no resources."""
    backend = _backend()
    assert backend.silence_hint == ""
    backend.close()
    backend.close()


def test_select_backend_picks_linux(monkeypatch):
    """The factory routes ``linux`` platforms to this backend."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(select_backend(), LinuxBackend)


def test_linux_backend_imports_no_audio_library():
    """The backend only shells out, so importing it must work without any audio package."""
    result = subprocess.run([sys.executable, "-c", _IMPORT_LINUX], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
