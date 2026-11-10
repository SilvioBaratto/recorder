"""Tests for argument dispatch and the no-audio CLI paths."""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from fake_openai import FakeClient
from recorder import chunker, cli, state, transcribe
from recorder.chunker import Chunk, ChunkerError

_FAKE_DAEMON = """
import os, sys, time
from recorder import state
part, payload, mode = sys.argv[1], sys.argv[2].encode(), sys.argv[3]
with open(part, "wb") as handle:
    handle.write(payload)
    handle.flush()
    while mode == "hang" or not state.stop_requested():
        time.sleep(0.05)
    os._exit(3)
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Isolate state so CLI tests never touch a real recording."""
    monkeypatch.setenv("RECORDER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_daemon(isolated_home):
    """Return a starter for a stand-in daemon that writes the .part but never publishes it.

    The stand-in holds the .part open like the real daemon. By default it dies the moment stop
    is signalled, as the real one did when PortAudio teardown crashed; with ``hang=True`` it
    ignores stop, so ``recorder stop`` has to kill it.
    """
    procs = []

    def start(payload: bytes, hang: bool = False) -> Path:
        """Launch the stand-in recording ``payload`` and register it as the active recording."""
        state.ensure_dirs()
        output = isolated_home / "audio" / "rec.mp3"
        part = isolated_home / "audio" / "rec.mp3.part"
        proc = subprocess.Popen([sys.executable, "-c", _FAKE_DAEMON, str(part), payload.decode(),
                                 "hang" if hang else "die"])
        procs.append(proc)
        deadline = time.monotonic() + 10
        while not (part.exists() and part.stat().st_size == len(payload)):
            assert time.monotonic() < deadline, "stand-in daemon never wrote its .part"
            time.sleep(0.02)
        state.write_state({"pid": proc.pid, "started_at": "now", "output": str(output)})
        return output

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


def test_requires_a_subcommand():
    """Bare invocation errors out instead of doing something surprising."""
    with pytest.raises(SystemExit):
        cli.main([])


def test_main_dispatches_to_handler(monkeypatch):
    """Each subcommand routes to its handler and returns that handler's code."""
    calls = []
    for name in ("cmd_start", "cmd_stop", "cmd_status", "cmd_devices"):
        monkeypatch.setattr(cli, name, lambda _a, _n=name: calls.append(_n) or 0)
    assert cli.main(["status"]) == 0
    assert calls == ["cmd_status"]


def test_status_reports_idle():
    """With no state, status prints idle and succeeds."""
    assert cli.cmd_status(None) == 0


def test_stop_without_recording_is_an_error():
    """Stopping when nothing is recording is a clean, non-zero outcome."""
    assert cli.cmd_stop(None) == 1


def test_start_refuses_when_already_active(monkeypatch):
    """A second start is refused while a live recording is tracked."""
    state.write_state({"pid": __import__("os").getpid(), "started_at": "now", "output": "x.mp3"})
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn a daemon")
    )
    assert cli.cmd_start(None) == 1


def test_stop_recovers_audio_when_daemon_dies_before_saving(fake_daemon):
    """A daemon that dies on stop without publishing still leaves the user their mp3."""
    output = fake_daemon(b"ID3 captured frames")
    assert cli.cmd_stop(None) == 0
    assert output.read_bytes() == b"ID3 captured frames"
    assert not Path(str(output) + ".part").exists()


def test_stop_recovers_audio_when_daemon_must_be_killed(fake_daemon, monkeypatch):
    """A daemon that ignores stop is killed, and the audio it already wrote is published."""
    monkeypatch.setattr(cli, "_STOP_TIMEOUT_SECONDS", 0.5)
    output = fake_daemon(b"ID3 captured frames", hang=True)
    assert cli.cmd_stop(None) == 0
    assert output.read_bytes() == b"ID3 captured frames"


def test_devices_prints_backend_description(capsys, monkeypatch):
    """``recorder devices`` prints whatever the platform backend reports, then releases it."""
    backend = FakeBackend()
    backend.describe = lambda: "X"
    monkeypatch.setattr(cli, "select_backend", lambda: backend)
    assert cli.cmd_devices(None) == 0
    assert "X" in capsys.readouterr().out
    assert backend.closed


def test_stop_prints_a_pending_warning(fake_daemon, capsys):
    """A warning the daemon left behind is shown after the saved line, then consumed."""
    fake_daemon(b"ID3 captured frames")
    state.write_warning("call side silent")
    assert cli.cmd_stop(None) == 0
    out = capsys.readouterr().out
    assert out.index("Saved") < out.index("Warning: call side silent")
    assert state.pop_warning() is None


def test_stop_publishes_before_reading_the_warning(fake_daemon, monkeypatch):
    """The recording is renamed into place before the side note is even looked at."""
    output = fake_daemon(b"ID3 captured frames")
    published_when_read = []
    real_pop_warning = state.pop_warning

    def pop_warning():
        """Note whether the mp3 already exists, then read the warning as usual."""
        published_when_read.append(output.exists())
        return real_pop_warning()

    monkeypatch.setattr(state, "pop_warning", pop_warning)
    assert cli.cmd_stop(None) == 0
    assert published_when_read == [True]


def test_stop_saves_despite_a_directory_at_the_warning_path(fake_daemon, capsys):
    """An unreadable warning must never cost the recording or turn a saved stop into a failure."""
    output = fake_daemon(b"ID3 captured frames")
    state.warning_file().mkdir(parents=True)
    assert cli.cmd_stop(None) == 0
    out = capsys.readouterr().out
    assert "Saved" in out
    assert "Warning:" not in out
    assert output.read_bytes() == b"ID3 captured frames"


def test_stop_saves_despite_an_undecodable_warning(fake_daemon, capsys):
    """A corrupt warning file is dropped; the recording is still saved and reported."""
    output = fake_daemon(b"ID3 captured frames")
    state.warning_file().write_bytes(b"\xff\xfe broken \x80")
    assert cli.cmd_stop(None) == 0
    out = capsys.readouterr().out
    assert "Saved" in out
    assert "Warning:" not in out
    assert output.read_bytes() == b"ID3 captured frames"
    assert not state.warning_file().exists()


def test_stop_discards_the_warning_when_no_mp3_was_produced(fake_daemon, capsys):
    """A warning about a recording that does not exist is dropped rather than shown."""
    fake_daemon(b"")
    state.write_warning("call side silent")
    assert cli.cmd_stop(None) == 1
    assert "call side silent" not in capsys.readouterr().out
    assert state.pop_warning() is None


def test_start_clears_a_stale_warning(monkeypatch):
    """A warning from an earlier recording must not leak into a new one."""

    class _Healthy:
        pid = os.getpid()

        def poll(self):
            """Report the daemon as still running."""
            return None

    state.write_warning("left over from the last recording")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_a, **_k: _Healthy())
    monkeypatch.setattr(cli, "_STARTUP_GRACE_SECONDS", 0.01)
    try:
        assert cli.cmd_start(None) == 0
        assert state.pop_warning() is None
    finally:
        state.clear_state()


def test_stop_does_not_publish_an_empty_recording(fake_daemon):
    """An empty .part means nothing was encoded: no mp3, and stop reports failure."""
    output = fake_daemon(b"")
    assert cli.cmd_stop(None) == 1
    assert not output.exists()
    assert not Path(str(output) + ".part").exists()


def test_start_reports_a_child_that_exits_immediately(monkeypatch, capsys):
    """A daemon that dies during the startup grace is reported as failed and leaves no state."""
    real_popen = subprocess.Popen
    spawned = []

    def exits_immediately(_cmd, **kwargs):
        proc = real_popen([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", exits_immediately)
    monkeypatch.setattr(cli, "_STARTUP_GRACE_SECONDS", 0.5)
    try:
        assert cli.cmd_start(None) == 1
        assert state.read_state() is None
        assert "failed to start" in capsys.readouterr().out
    finally:
        for proc in spawned:
            proc.wait()


@pytest.fixture
def pipeline(monkeypatch):
    """Run ``recorder transcribe`` offline: fake extras check, chunk source and OpenAI client.

    Tests adjust ``run.script`` (client outcomes) and ``run.chunk_error`` before invoking the
    command; ``run.decoded`` lists the files the chunker was asked to read and ``run.client``
    is the client the command created.
    """
    run = SimpleNamespace(
        decoded=[],
        client=None,
        script=["perché sì", "", "così"],
        chunk_error=None,
        chunks=[Chunk(0.0, 1200.0, b"a"), Chunk(1200.0, 600.0, b"b"), Chunk(1800.0, 60.0, b"c")],
    )

    def fake_iter_chunks(path, **_kwargs):
        """Record the requested file, then fail or yield the canned chunks."""
        run.decoded.append(path)
        if run.chunk_error:
            raise run.chunk_error
        yield from run.chunks

    def fake_make_client():
        """Hand out a client scripted with ``run.script``."""
        run.client = FakeClient(run.script)
        return run.client

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_missing_extras", lambda: [])
    monkeypatch.setattr(chunker, "iter_chunks", fake_iter_chunks)
    monkeypatch.setattr(transcribe, "make_client", fake_make_client)
    return run


def _recording(name="rec.mp3", mtime=None):
    """Create a stand-in mp3 under the isolated audio folder and return its path."""
    path = state.audio_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_transcribe_picks_newest_mp3_and_ignores_part(pipeline, capsys):
    """With no path, the newest finished mp3 is used and an in-progress .part never is."""
    _recording("old.mp3", 1000)
    newest = _recording("new.mp3", 2000)
    _recording("rec.mp3.part", 3000)
    assert cli.main(["transcribe"]) == 0
    assert pipeline.decoded == [newest]
    assert newest.with_suffix(".txt").exists()
    assert not (state.audio_dir() / "old.txt").exists()
    assert str(newest.with_suffix(".txt")) in capsys.readouterr().out


def test_transcribe_without_any_mp3_is_an_error(pipeline, capsys):
    """An empty audio folder gets a plain message, not a traceback."""
    assert cli.main(["transcribe"]) == 1
    assert "No mp3" in capsys.readouterr().out
    assert pipeline.decoded == []


def test_transcribe_refuses_existing_txt_without_force(pipeline, capsys):
    """A finished transcript is never overwritten by accident; no decoding or API call happens."""
    recording = _recording()
    transcript = recording.with_suffix(".txt")
    transcript.write_text("old", encoding="utf-8")
    assert cli.main(["transcribe"]) == 1
    assert "--force" in capsys.readouterr().out
    assert transcript.read_text(encoding="utf-8") == "old"
    assert pipeline.decoded == []


def test_transcribe_force_overwrites(pipeline):
    """--force replaces an existing transcript."""
    recording = _recording()
    transcript = recording.with_suffix(".txt")
    transcript.write_text("old", encoding="utf-8")
    assert cli.main(["transcribe", "--force"]) == 0
    assert transcript.read_text(encoding="utf-8") == "perché sì\n\ncosì"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_transcribe_missing_key_fails_before_decoding(pipeline, monkeypatch, capsys, value):
    """No usable key is reported up front, before any audio is decoded."""
    _recording()
    if value is None:
        monkeypatch.delenv("OPENAI_API_KEY")
    else:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    assert cli.main(["transcribe"]) == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().out
    assert pipeline.decoded == []


def test_transcribe_missing_extras_fails_with_install_hint(pipeline, monkeypatch, capsys):
    """Without the optional libraries the user is told exactly what to install."""
    _recording()
    monkeypatch.setattr(cli, "_missing_extras", lambda: ["miniaudio"])
    assert cli.main(["transcribe"]) == 1
    assert 'pip install -e ".[transcribe]"' in capsys.readouterr().out
    assert pipeline.decoded == []


def test_transcribe_languages_and_keywords_reach_the_client(pipeline):
    """Comma-separated flags are split, stripped and passed as hints."""
    _recording()
    argv = ["transcribe", "--languages", "it, en", "--keywords", "AC-42, Premium Plus"]
    assert cli.main(argv) == 0
    assert pipeline.client.calls[0]["extra_body"] == {
        "languages": ["it", "en"],
        "keywords": ["AC-42", "Premium Plus"],
    }


@pytest.mark.parametrize("keyword", ["a<b", "a>b", "a\nb"])
def test_transcribe_rejects_unsafe_keywords_before_decoding(pipeline, capsys, keyword):
    """The API rejects the whole request over these characters, so fail early and clearly."""
    _recording()
    assert cli.main(["transcribe", "--keywords", keyword]) == 1
    assert "keyword" in capsys.readouterr().out.lower()
    assert pipeline.decoded == []


def test_transcribe_success_writes_utf8_and_reports_progress(pipeline, capsys):
    """The transcript is UTF-8; the console only ever sees the path and counts."""
    recording = _recording()
    assert cli.main(["transcribe"]) == 0
    assert recording.with_suffix(".txt").read_text(encoding="utf-8") == "perché sì\n\ncosì"
    out = capsys.readouterr().out
    assert "chunk 1 done (20:00 of audio)" in out
    assert "chunk 3 done (31:00 of audio)" in out
    assert "3 chunks" in out
    assert "perché" not in out


def test_transcribe_all_silent_recording_reports_no_speech(pipeline, capsys):
    """A recording with no speech is a likely device problem, so say so instead of saving an empty file."""
    recording = _recording()
    pipeline.script = ["", "", ""]
    assert cli.main(["transcribe"]) == 1
    assert "No speech" in capsys.readouterr().out
    assert not recording.with_suffix(".txt").exists()


def test_transcribe_failure_keeps_a_partial_transcript(pipeline, capsys):
    """A chunk that still fails after retries leaves what was done, and names where it stopped."""
    recording = _recording()
    pipeline.script = ["first", RuntimeError("boom")]
    assert cli.main(["transcribe"]) == 1
    out = capsys.readouterr().out
    assert "chunk 2" in out and "20:00" in out and "boom" in out
    assert Path(str(recording.with_suffix(".txt")) + ".partial").read_text(encoding="utf-8") == "first"
    assert not recording.with_suffix(".txt").exists()


def test_transcribe_ctrl_c_keeps_a_partial_transcript(pipeline):
    """Interrupting the run exits 130 and still saves the finished chunks."""
    recording = _recording()
    pipeline.script = ["first", KeyboardInterrupt()]
    assert cli.main(["transcribe"]) == 130
    partial = Path(str(recording.with_suffix(".txt")) + ".partial")
    assert partial.read_text(encoding="utf-8") == "first"


def test_transcribe_auth_error_points_at_the_key(pipeline, capsys):
    """A rejected key is explained, and the raw API message is not echoed."""
    _recording()
    rejected = RuntimeError("Incorrect API key provided: sk-...abcd")
    rejected.status_code = 401
    pipeline.script = [rejected]
    assert cli.main(["transcribe"]) == 1
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out
    assert "sk-" not in out


def test_transcribe_reports_a_chunker_error(pipeline, capsys):
    """Decoding problems are shown as one clear line and leave no files behind."""
    recording = _recording()
    pipeline.chunk_error = ChunkerError("no audio found in rec.mp3")
    assert cli.main(["transcribe"]) == 1
    assert "no audio found" in capsys.readouterr().out
    assert not list(state.audio_dir().glob("rec.txt*"))
    assert recording.exists()


def test_transcribe_rejects_a_missing_or_directory_path(pipeline, capsys, isolated_home):
    """A bad path argument is a one-line message, not a traceback."""
    assert cli.main(["transcribe", str(isolated_home / "nope.mp3")]) == 1
    assert "not found" in capsys.readouterr().out.lower()
    assert cli.main(["transcribe", str(isolated_home)]) == 1
    assert "not a file" in capsys.readouterr().out.lower()
    assert pipeline.decoded == []


def test_transcribe_accepts_a_path_with_spaces_and_accents(pipeline):
    """The explicit path form works for folders and names like the ones real users have."""
    recording = _recording("cartella è/riunione 1.mp3")
    assert cli.main(["transcribe", str(recording)]) == 0
    assert recording.with_suffix(".txt").exists()
    assert pipeline.decoded == [recording]


def test_status_does_not_import_the_transcribe_libraries(isolated_home):
    """Quick commands stay quick: openai and miniaudio load only when transcribing."""
    code = (
        "import sys\n"
        "from recorder import cli\n"
        "cli.main(['status'])\n"
        "print('openai' in sys.modules, 'miniaudio' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines()[-1] == "False False"
