# Cross-platform capture (macOS + Linux) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `recorder start | stop | status | devices` record call audio + microphone to one mp3 on Windows (unchanged), macOS 14.2+ and PulseAudio/PipeWire Linux.

**Architecture:** Capture sits behind a `Source`/`Backend` interface (`src/recorder/backends/`). `DeviceStream` consumes any `Source` through push callbacks and keeps the shared resample/buffer logic. Windows keeps PyAudioWPatch, macOS uses a `catap` Core Audio tap plus `sounddevice`, Linux uses `parec` subprocesses. Mixer, encoder, daemon and state protocol are shared.

**Tech Stack:** Python (3.9 floor), numpy, lameenc, PyAudioWPatch (Windows), catap + sounddevice (macOS), parec/pactl (Linux), pytest, uv.

**Spec:** `docs/superpowers/specs/2026-11-07-cross-platform-capture-design.md`

## Global Constraints

- `requires-python` stays `>=3.9`. All code must import and run on 3.9: `from __future__ import annotations`, `typing.Optional`/`Callable` for runtime-evaluated aliases, no `match`, no `dataclass(slots=/kw_only=)`.
- Dependencies, exactly: `numpy>=1.23`, `lameenc>=1.7`, `PyAudioWPatch>=0.2.12.7; sys_platform == 'win32'`, `sounddevice>=0.4.6; sys_platform == 'darwin'`, `catap>=0.6.1,<0.7; sys_platform == 'darwin' and python_version >= '3.11'`.
- macOS capture needs macOS 14.2+ and Python 3.11+; failures are `RuntimeError`s with an actionable message, never a traceback-only crash.
- Never block in a device `read()`: readers poll for availability. The mp3 is published (`.part` renamed) **before** sources are closed, and sources are closed before `backend.close()`.
- Linux `parec` flags: `--format=float32le --rate=48000 --channels=2`. Shutdown is `SIGTERM`, then `SIGKILL` after a short wait.
- The `os.name == "nt"` branches of `pid_alive` and `terminate` are untouched. The Windows backend is a pure move of today's code.
- Importing `recorder.cli`, `recorder.engine`, `recorder.audio_io` or `recorder.backends` must not import `pyaudiowpatch`, `sounddevice` or `catap`.
- Comments explain WHY only (no WHAT, no task narration, no TODO). Public APIs get Google-style docstrings (meaning of params, returns, raises). English only.
- Test command (repo path contains a space, always quote): `"/Volumes/External SSD/recorder/.venv/bin/pytest" -q`.
- End every commit message with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Spec amendments made by this plan

Applied to the spec in Task 9. Each is a decision the spec left open or got slightly wrong:
1. `parec` also gets `--latency-msec=100`, so both streams arrive in ~0.1 s bursts matching `BLOCK`.
2. `pactl info` runs with `LC_ALL=C`; otherwise a localized system (e.g. Italian) prints `Sink di default:` and parsing fails.
3. A default source whose name ends in `.monitor` (or no default source) is rejected as a microphone: recording the monitor twice would double the call.
4. `pid_alive` reaps an exited child with `os.waitpid(pid, WNOHANG)` before `os.kill(pid, 0)`, because a zombie child still answers signal 0. `terminate` uses the process group only when the pid leads its own group (`os.getpgid(pid) == pid`); `killpg` on a non-leader would hit the wrong group.
5. `recorder stop` learns about the silent-call warning through `.state/warning.txt` (`stop` runs in another process than the daemon).
6. `PolledSource` (`backends/polled.py`) is shared by the Windows source and the macOS microphone.
7. macOS `open_loopback()` starts the tap immediately (its format is only known after start); buffers arriving before `Source.start()` are discarded.

## Review Focus

Inputs the spec implies but never pins, most likely to bite first, each with its owning test:
1. **16 kHz mono headset mic** on any OS must come out as 48 kHz stereo and mix: `test_16k_mono_source_comes_out_as_48k_stereo` (Task 4).
2. **Non-English Linux locale** breaks `pactl info` parsing: `test_pactl_runs_with_c_locale` (Task 6).
3. **Linux default source is a `.monitor`, or no mic at all** (desktop PC): must fail loudly, not record the call twice: `test_default_source_monitor_is_rejected`, `test_missing_default_source_is_rejected` (Task 6).
4. **`pip install` under this Mac's system `python3` 3.9.6** installs fine without `catap`; `recorder start` must say "Python 3.11+", not crash: `test_check_preconditions_*` (Task 8).
5. **This repo lives in `/Volumes/External SSD/recorder`** (space in the path) with a detached daemon: exercised end to end in Task 10.

---

### Task 1: Dev environment and per-platform dependencies

**Files:**
- Modify: `pyproject.toml:11-18`, `.gitignore`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Produces: a working `.venv` (Python 3.12) for every later task; dependency markers per the Global Constraints.

- [ ] **Step 1: Write the failing test** `tests/test_packaging.py::test_dependencies_per_platform`. Read `importlib.metadata.requires("recorder")`, parse each with `packaging.requirements.Requirement`, evaluate markers with `{"sys_platform": p, "python_version": v, "extra": ""}`, compare canonical-name sets. Expected: `win32/3.12` = {numpy, lameenc, pyaudiowpatch}; `linux/3.12` = {numpy, lameenc}; `darwin/3.12` = {numpy, lameenc, sounddevice, catap}; `darwin/3.10` = {numpy, lameenc, sounddevice}.
- [ ] **Step 2: Rewrite the `dependencies` list** in `pyproject.toml` exactly as in Global Constraints; add `packaging` to the `dev` extra; add `.venv*/` to `.gitignore`.
- [ ] **Step 3: Create the env.** Run `uv venv --python 3.12 .venv` then `uv pip install --python .venv/bin/python -e ".[dev]"`. Expected: succeeds (before this change it failed on `PyAudioWPatch`). Then `.venv/bin/python -c "import catap, sounddevice, numpy, lameenc"` exits 0.
- [ ] **Step 4: Record the baseline.** Run `.venv/bin/pytest -q`. Expected: `test_packaging`, `test_cli`, `test_state`, `test_mix`, `test_resample` pass; `test_audio_io` and `test_daemon` fail at import with `ModuleNotFoundError: pyaudiowpatch` (fixed in Task 4). Note how long `test_cli` takes (zombie waits, fixed in Task 5).
- [ ] **Step 5: Commit** `build: per-platform audio dependencies`.

---

### Task 2: Backend interfaces and `select_backend`

**Files:**
- Create: `src/recorder/backends/__init__.py`, `src/recorder/backends/base.py`, `tests/test_backends.py`

**Interfaces:**
- Produces (`base.py`): `OnBlock = Callable[[np.ndarray], None]` (float32, shape `(n, channels)`, native rate); `OnEnd = Callable[[Optional[BaseException]], None]` (called exactly once by the source's thread when delivery stops; `None` means a normal close); `@dataclass(frozen=True) SourceInfo(name: str, sample_rate: int, channels: int)`; `Source` protocol with `info: SourceInfo`, `start(on_block: OnBlock, on_end: OnEnd) -> None`, `close() -> None` (idempotent, never blocks on a device read); `Backend` protocol with `silence_hint: str` (empty = no warning), `open_loopback() -> Source`, `open_microphone() -> Source`, `describe() -> str`, `close() -> None`.
- Produces (`__init__.py`): `select_backend() -> Backend`, dispatching on `sys.platform`: `win32` → `.windows.WindowsBackend()`, `darwin` → `.macos.MacBackend()`, `linux*` → `.linux.LinuxBackend()`, each imported lazily inside the branch; anything else raises `RuntimeError` containing the platform string.

- [ ] **Step 1: Write the failing test** `test_backends.py::test_unsupported_platform_raises`: `monkeypatch.setattr(sys, "platform", "freebsd13")`; `pytest.raises(RuntimeError, match="freebsd13")`.
- [ ] **Step 2: Run it.** Expected: FAIL (`ModuleNotFoundError: recorder.backends`).
- [ ] **Step 3: Implement** `base.py` and `__init__.py` per the Interfaces. Docstrings state the `on_end`-exactly-once and idempotent-`close` contracts.
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: backend and source interfaces`.

---

### Task 3: Shared polling source

**Files:**
- Create: `src/recorder/backends/polled.py`, `tests/test_polled.py`

**Interfaces:**
- Consumes: `SourceInfo`, `OnBlock`, `OnEnd` from Task 2.
- Produces: `class PolledSource` implementing `Source`, `__init__(self, info: SourceInfo, available: Callable[[], int], read: Callable[[int], np.ndarray], close_device: Callable[[], None], start_device: Callable[[], None] = <no-op>, poll_seconds: float = 0.01)`. `start()` calls `start_device()` then runs a daemon thread: loop until closed: `n = available()`; if 0, `closed_event.wait(poll_seconds)`; else `on_block(read(n))`. On exception it remembers it. In the thread's `finally`: `close_device()` (once), then `on_end(error)`. `close()` sets the event, joins the thread (timeout 2 s), and if the thread never started calls `close_device()` itself; idempotent.

- [ ] **Step 1: Write the failing tests** (`tests/test_polled.py`, fakes defined inline):
  - `test_delivers_blocks_until_closed`: `available` returns 4 then 0 forever; `read` returns zeros `(4, 2)`; after `close()`, `on_block` got ≥1 block, `on_end` got `None` exactly once, `close_device` called once.
  - `test_never_reads_when_nothing_is_available`: `available` always 0, `read` raises `AssertionError`; `close()` returns within 1 s; `on_end` got `None`.
  - `test_read_error_is_reported_through_on_end`: `read` raises `RuntimeError("boom")`; `on_end` receives that exception; `close_device` called once.
  - `test_close_is_idempotent_and_safe_before_start`: build, `close()` twice without `start()`; `close_device` called exactly once, `on_end` never called.
  - `test_start_device_runs_before_first_read`: record call order `["start_device", "read"]`.
- [ ] **Step 2: Run them.** Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `PolledSource` per the Interfaces. The docstring explains why it polls (a silent loopback never returns from `read()`, and a thread parked inside a native read crashes teardown).
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: shared polling source`.

---

### Task 4: Route capture through backends (Windows unchanged)

**Files:**
- Create: `src/recorder/backends/windows.py`, `tests/fake_backend.py`, `tests/test_windows_backend.py`
- Modify: `src/recorder/audio_io.py`, `src/recorder/engine.py`, `src/recorder/cli.py` (`cmd_devices`), `scripts/diag.py`, `tests/fake_pyaudio.py`, `tests/test_audio_io.py`, `tests/test_daemon.py`, `tests/test_cli.py`, `tests/test_backends.py`

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces (`windows.py`): `class WindowsBackend` with `silence_hint = ""`; `__init__(self, pyaudio_module: Any = None)` (lazy: imports `pyaudiowpatch` and creates `PyAudio()` only on first `open_*`/`describe`); overridable `find_loopback_device(self) -> dict`, `find_input_device(self) -> dict`, `_loopback_devices(self) -> Iterable[dict]` (logic moved verbatim from `audio_io.py:29-48`, using `self._module.paWASAPI`); `open_loopback()`/`open_microphone()` open a PortAudio input stream exactly as `DeviceStream.__init__` did (`paFloat32`, `channels=maxInputChannels`, `rate=defaultSampleRate`, `frames_per_buffer=max(256, rate // 10)`, `input=True`, `input_device_index`) and wrap it in a `PolledSource` (`available=stream.get_read_available`, `read` = `np.frombuffer(stream.read(n, exception_on_overflow=False), dtype=np.float32).reshape(-1, channels)`, `close_device` = `stop_stream()` then `close()`); `describe()` returns the old `describe_devices` text byte for byte; `close()` calls `pa.terminate()` only if a `PyAudio` was created.
- Produces (`audio_io.py`): `DeviceStream(source: Source, target_rate: int, stop_event: threading.Event, log: Log, label: str)` with `start()` (→ `source.start(self._on_block, self._on_end)`), `pull(frames)`, `pull_nowait(frames)`, `join()` (→ `source.close()`; no timeout argument). `_on_block` does what the old loop body did (`_to_stereo`, `LinearResampler`, append under the condition, 10 s cap). `_on_end(error)`: if `error`, `log(f"{label} capture error: {error!r}")` and `stop_event.set()`; then mark finished and notify. `open_pyaudio`, `find_*`, `describe_devices` and the `pyaudiowpatch` import are deleted from this module.
- Produces (`engine.py`): `from .backends import select_backend` at module level; `record(output, stop_event, log, samplerate, blocksize) -> int` signature unchanged.
- Produces (tests): `tests/fake_backend.py` with `FakeSource(name="fake", rate=48000, channels=2, mode="live", level=0.1)` (modes: `live` pushes `level`-filled blocks of rate/10 frames at 10× real time from its own thread; `silent` never pushes; `close()` idempotent, sets `closed`, calls `on_end(None)` once) and `FakeBackend(loopback=None, microphone=None, silence_hint="", fail_microphone=False)` recording `closed`; `tests/fake_pyaudio.py` gains `FakePyAudioModule(pa)` (`paFloat32 = 1`, `paWASAPI = 13`, `PyAudio()` returns `pa`) and `FakeWindowsBackend(WindowsBackend)` overriding `find_loopback_device` → `LOOPBACK`, `find_input_device` → `MIC`, `_loopback_devices` → `[LOOPBACK]`.

- [ ] **Step 1: Write/migrate the failing tests.**
  - `test_audio_io.py::test_silent_device_releases_its_stream_on_stop`: same assertions as today, built via `FakeWindowsBackend(FakePyAudioModule(pa)).open_loopback()` wrapped in `DeviceStream`.
  - `test_audio_io.py::test_16k_mono_source_comes_out_as_48k_stereo`: `FakeSource(rate=16000, channels=1)`; run ~0.5 s of audio; `pull(...)` blocks sum to `24000 ± 2` frames and every block has shape `(n, 2)`.
  - `test_audio_io.py::test_source_error_stops_the_recording`: a source that calls `on_end(RuntimeError("boom"))`; afterwards `stop_event.is_set()`, the log contains `"capture error"`, and `pull(100)` returns without blocking.
  - `test_daemon.py`: the `devices` fixture builds `FakeWindowsBackend(FakePyAudioModule(pa))` and does `monkeypatch.setattr(engine, "select_backend", lambda: backend)`; the two existing tests keep their assertions.
  - `test_daemon.py::test_opening_failure_closes_already_opened_sources`: `monkeypatch.setattr(engine, "select_backend", lambda: backend)` with `FakeBackend(fail_microphone=True)`; `engine.record(...)` raises `RuntimeError`; the loopback `FakeSource.closed` is true and `backend.closed` is true.
  - `test_cli.py::test_devices_prints_backend_description(capsys, monkeypatch)`: `cli.select_backend` returns a backend whose `describe()` is `"X"`; output contains `X`; backend closed.
  - `test_backends.py::test_core_modules_import_no_audio_library`: in a subprocess (`sys.executable -c`), import `recorder.cli, recorder.engine, recorder.audio_io, recorder.backends` and assert `{"pyaudiowpatch","sounddevice","catap"}.isdisjoint(sys.modules)`.
  - `test_windows_backend.py::test_describe_keeps_the_existing_format`: with `FakeWindowsBackend`, output equals the three expected lines `Call loopback source : Fake Speakers [Loopback] @ 48000 Hz`, `Microphone (your voice): Fake Microphone @ 48000 Hz`, and a `All loopback endpoints:` block with `  - Fake Speakers [Loopback]`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** `windows.py`, the `DeviceStream` rewrite, `engine.record` (open both sources through the backend; if the second open raises, close the first and re-raise; `backend.close()` in the outermost `finally`; log lines keep the old `loopback = ... @ ... Hz` shape from `source.info`), `cli.cmd_devices` (`backend = select_backend()`, print `describe()`, `backend.close()` in `finally`; import `select_backend` at module level), and port `scripts/diag.py` to `backend.open_loopback()/open_microphone()` + `DeviceStream` (`py_compile` must pass).
- [ ] **Step 4: Run the full suite.** Expected: ALL tests pass on macOS with no `pyaudiowpatch` installed. Run `.venv/bin/python -m py_compile scripts/diag.py`.
- [ ] **Step 5: Review the Windows diff.** Compare `windows.py` against the removed `audio_io.py:24-64,97-104` line by line; any behavioural difference is a bug (Windows cannot be run here).
- [ ] **Step 6: Commit** `refactor: route capture through backends`.

---

### Task 5: POSIX process-control hardening

**Files:**
- Modify: `src/recorder/state.py` (`pid_alive`, `terminate`), `src/recorder/cli.py` (`cmd_start`)
- Test: `tests/test_state.py`, `tests/test_cli.py` (POSIX-only tests use `pytest.mark.skipif(os.name == "nt", ...)`)

**Interfaces:**
- Produces: `state._TERM_GRACE_SECONDS = 2.0`; `pid_alive(pid)` on POSIX first tries `os.waitpid(pid, os.WNOHANG)` (a returned pid means an exited child was reaped → `False`; `ChildProcessError` → not our child, continue), then `os.kill(pid, 0)` where `ProcessLookupError` → `False` and `PermissionError` → `True`; `terminate(pid)` on POSIX sends `SIGTERM`, polls `pid_alive` for `_TERM_GRACE_SECONDS`, then `SIGKILL`, each through `_signal(pid, sig)`, which targets `os.killpg(pid, sig)` only when `os.getpgid(pid) == pid` and otherwise `os.kill(pid, sig)`, swallowing `ProcessLookupError`/`PermissionError`; `cmd_start` decides "failed to start" from `proc.poll() is not None` instead of `pid_alive`.

- [ ] **Step 1: Write the failing tests.**
  - `test_pid_alive_is_true_when_signalling_is_not_permitted`: patch `state.os.kill` to raise `PermissionError`, `os.waitpid` to raise `ChildProcessError`; assert `pid_alive(4242)`.
  - `test_pid_alive_is_false_for_an_exited_child`: spawn `python -c pass` and do not `wait()` on it; poll `pid_alive(proc.pid)` for up to 5 s and assert it becomes `False` (it stays `True` today because the unreaped zombie still answers signal 0).
  - `test_terminate_kills_the_whole_process_group`: spawn a parent with `start_new_session=True` that starts a `sleep 60` child and prints the child's pid; `terminate(parent.pid)`; both pids gone within 5 s.
  - `test_terminate_does_not_kill_a_foreign_group`: spawn a sleeper *without* a new session; assert `os.getpgid(pid) != pid`; `terminate(pid)`; the sleeper is dead and the test process is still alive.
  - `test_terminate_escalates_to_sigkill`: child installs `signal.SIG_IGN` for `SIGTERM`; set `_TERM_GRACE_SECONDS = 0.3`; child dead afterwards.
  - `test_start_reports_a_child_that_exits_immediately`: patch `cli.subprocess.Popen` to launch `[sys.executable, "-c", "raise SystemExit(1)"]`, `_STARTUP_GRACE_SECONDS = 0.5`; `cmd_start(None) == 1` and `read_state()` is `None`.
- [ ] **Step 2: Run them.** Expected: FAIL (the zombie and group cases fail today).
- [ ] **Step 3: Implement** per Interfaces. Comment the WHY of `waitpid` (zombie answers signal 0) and of the `getpgid == pid` guard (`killpg` on a non-leader hits another group).
- [ ] **Step 4: Run the full suite.** Expected: PASS, and `test_cli.py` is much faster than the Task 1 baseline.
- [ ] **Step 5: Commit** `fix: POSIX pid liveness and process-group termination`.

---

### Task 6: Linux backend

**Files:**
- Create: `src/recorder/backends/linux.py`, `tests/fake_parec.py`, `tests/test_linux_backend.py`

**Interfaces:**
- Consumes: `Source`, `SourceInfo`, `config.SAMPLERATE`.
- Produces: `parse_pactl_info(text: str) -> tuple[Optional[str], Optional[str]]` (`Default Sink:`, `Default Source:` lines, `(sink, source)`); `class ParecSource` implementing `Source`: `__init__(self, device: str, spawn: Callable = subprocess.Popen)`, `info = SourceInfo(device, SAMPLERATE, 2)`; spawns `["parec", f"--device={device}", "--format=float32le", "--rate=48000", "--channels=2", "--latency-msec=100"]` with `bufsize=0`, stdout and stderr piped; reader thread does `stdout.read(65536)`, carries leftover bytes modulo 8 across reads, `np.frombuffer(..., "<f4").reshape(-1, 2)`; EOF while not closing → `on_end(RuntimeError(...))` including the exit code and parec's stderr; `close()` does `terminate()`, `wait(timeout=1)`, `kill()` on timeout, joins the reader, idempotent. `class LinuxBackend` with `silence_hint = ""`, `__init__(self, which=shutil.which, run=subprocess.run, spawn=subprocess.Popen)`, `open_loopback()` → `ParecSource(f"{sink}.monitor")`, `open_microphone()` → `ParecSource(source)`, `describe()` (same three-section shape as the Windows text; problems rendered as `UNAVAILABLE (...)`, never raised), `close()` no-op.
- Errors (all `RuntimeError`): missing `pactl`/`parec` → message contains `pulseaudio-utils` (and `libpulse` for Arch); no default sink → contains `no default output`; default source missing or ending `.monitor` → contains `no microphone`.
- `pactl info` runs as `run(["pactl", "info"], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})`.

- [ ] **Step 1: Write the failing tests** (`fake_parec.py` provides `FakeProc` with a scripted stdout chunk list then EOF, `terminate/kill/wait/poll`, stderr text, and a `spawn` recorder):
  - `test_parse_pactl_info_extracts_defaults`: sample English output → `("alsa_output.x.analog-stereo", "alsa_input.y.analog-stereo")`.
  - `test_pactl_runs_with_c_locale`: the fake `run` captures `env`; `env["LC_ALL"] == "C"` (Review Focus 2).
  - `test_frames_are_reassembled_across_split_reads`: stdout yields 5 bytes, then 11, then 8 bytes of float32 stereo (3 frames total); `on_block` receives 3 frames overall in order, none partial.
  - `test_parec_command_line`: spawned argv equals the list in Interfaces for device `"sink.monitor"`.
  - `test_close_terminates_then_ends_cleanly`: `close()` calls `terminate()` once, `on_end(None)` once, a second `close()` is a no-op.
  - `test_close_escalates_to_kill`: `wait` raises `TimeoutExpired` once; `kill()` called.
  - `test_unexpected_exit_is_reported`: EOF without `close()` → `on_end` gets a `RuntimeError` mentioning the exit code and stderr text.
  - `test_missing_tools_name_the_package`: `which` returns `None` → message contains `pulseaudio-utils`.
  - `test_default_source_monitor_is_rejected`: source `"alsa_output.x.monitor"` → `RuntimeError` containing `no microphone` (Review Focus 3).
  - `test_missing_default_source_is_rejected`: `Default Source:` absent or `n/a` → same error.
  - `test_describe_reports_problems_instead_of_raising`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces. Comment WHY for `LC_ALL=C`, the `.monitor` guard (would record the call twice), `bufsize=0`, and the leftover-bytes carry.
- [ ] **Step 4: Run the full suite.** Expected: PASS (on macOS; no `parec` needed).
- [ ] **Step 5: Commit** `feat: linux parec backend`.

---

### Task 7: Silent call-side warning

**Files:**
- Modify: `src/recorder/audio_io.py` (`DeviceStream.heard_signal`), `src/recorder/engine.py`, `src/recorder/state.py`, `src/recorder/cli.py` (`cmd_start`, `cmd_stop`)
- Test: `tests/test_daemon.py`, `tests/test_state.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `DeviceStream.heard_signal -> bool` (property; true once any delivered block had a non-zero sample; set in `_on_block` via `block.any()`); `state.warning_file() -> Path` (`.state/warning.txt`), `state.write_warning(message: str) -> None`, `state.pop_warning() -> Optional[str]` (read and delete), `state.clear_warning() -> None`. In `engine.record`, after the mp3 is published and before sources close: if `frames` and `not loop_stream.heard_signal` and `backend.silence_hint`, then `log(f"WARNING: {hint}")` and `state.write_warning(hint)`. `cmd_start` calls `clear_warning()` before spawning; `cmd_stop` prints `Warning: <text>` after the `Saved ...` line when `pop_warning()` returns text.

- [ ] **Step 1: Write the failing tests.**
  - `test_state.py::test_warning_roundtrip_and_pop_deletes`: write, pop returns text, second pop is `None`; `clear_warning` on absence does not raise.
  - `test_daemon.py::test_silent_call_side_leaves_a_warning`: `FakeBackend(loopback=FakeSource(mode="live", level=0.0), silence_hint="grant System Audio Recording")`; after a run, `state.pop_warning()` contains the hint and `engine.log` contains `WARNING`; the mp3 still exists.
  - `test_daemon.py::test_audible_call_side_leaves_no_warning`: loopback `level=0.1` → `pop_warning()` is `None`.
  - `test_daemon.py::test_empty_hint_never_warns`: silent loopback, `silence_hint=""` → `pop_warning()` is `None` (Windows/Linux behaviour).
  - `test_cli.py::test_stop_prints_a_pending_warning`: with the `fake_daemon` fixture and `write_warning("call side silent")` before `cmd_stop`, `capsys` output contains `call side silent`.
  - `test_cli.py::test_start_clears_a_stale_warning`: patch `Popen` to return an object with `pid = os.getpid()` and `poll() -> None`, shrink `_STARTUP_GRACE_SECONDS`; after `cmd_start`, `pop_warning()` is `None`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run the full suite.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: warn when the call side was silent`.

---

### Task 8: macOS backend

**Files:**
- Create: `src/recorder/backends/macos.py`, `tests/test_macos_backend.py`
- Modify: `tests/test_backends.py` (dispatch tests)

**Interfaces:**
- Consumes: `PolledSource`, `Source`, `SourceInfo`.
- Produces: `check_preconditions(mac_version: tuple[int, ...], python_version: tuple[int, int], catap_available: bool) -> None` raising `RuntimeError` whose text contains `14.2` (old macOS), `3.11` (old Python), or `pip install catap` (missing package), checked in that order; `convert_buffer(data: bytes, sample_type: str, bits: int, channels: int) -> np.ndarray` returning float32 `(n, channels)` (`"float"`/32 as-is, `"signed_integer"`/16 ÷ 32768, /32 ÷ 2147483648, anything else `ValueError`); `class MacBackend` with `silence_hint` = text containing `System Audio Recording` and telling the user to restart the terminal after granting it; `__init__(self, catap_module=None, sounddevice_module=None, mac_version=None, python_version=None)` (defaults from `platform.mac_ver()` / `sys.version_info`, modules imported lazily); `open_loopback()` runs the preconditions, creates a streaming-only `catap.record_system_audio(...)` session with an `on_buffer` that forwards to a mutable delegate (dropping buffers until `Source.start()`), starts it, reads `session.stream_format` to fill `SourceInfo`, and returns a source whose `close()` calls `session.stop()` from the owning thread (never inside `on_buffer`) then `on_end(None)`; a capture failure reported by the session is passed to `on_end(RuntimeError(...))`; `open_microphone()` uses `sounddevice.query_devices(kind="input")` and `InputStream(samplerate=round(default_samplerate), channels=min(max_input_channels, 2), dtype="float32")` wrapped in a `PolledSource` (`available = lambda: stream.read_available`, `read = lambda n: stream.read(n)[0]`, `start_device = stream.start`, `close_device` = `stop()` then `close()`); no input device → `RuntimeError` mentioning System Settings > Sound > Input; `describe()` never raises (problems as `UNAVAILABLE (...)`) and ends with a reminder to grant the terminal System Audio Recording and Microphone access; `close()` no-op.

- [ ] **Step 1: Confirm the `catap` API.** Run `.venv/bin/python -c "import catap, inspect; print(inspect.signature(catap.record_system_audio)); print([n for n in dir(catap.RecordingSession) if not n.startswith('_')])"`. Expected: `record_system_audio` accepts `on_buffer` with an optional/omittable output path, and the session exposes `start`, `stop`, `stream_format`, and a capture-failure flag or wait method. If a name differs, **stop and amend the spec** before writing tests; adapt Interfaces and the fake accordingly.
- [ ] **Step 2: Write the failing tests** (fakes defined inline: a fake `catap` module whose session calls the stored `on_buffer` with `SimpleNamespace(data, frame_count, format=SimpleNamespace(sample_rate, num_channels, bits_per_sample, sample_type))`; a fake `sounddevice` with `query_devices`, `InputStream`):
  - `test_check_preconditions_old_macos`: `((13, 6), (3, 12), True)` → message contains `14.2`.
  - `test_check_preconditions_old_python`: `((15, 0), (3, 9), False)` → message contains `3.11` (Review Focus 4; precedence over the missing package).
  - `test_check_preconditions_missing_catap`: `((15, 0), (3, 12), False)` → contains `pip install catap`; `((15, 0), (3, 12), True)` → returns `None`.
  - `test_convert_buffer_formats`: float32 stereo round-trips; int16 `[16384]` → `0.5`; int32 `[1<<30]` → `0.5`; mono shape `(n, 1)`; 24-bit raises `ValueError`.
  - `test_loopback_streams_converted_blocks_and_stops_once`: buffers pushed after `Source.start` reach `on_block` as float32; a buffer pushed before `start` is dropped; `close()` calls `session.stop()` once and `on_end(None)` once.
  - `test_loopback_capture_failure_reaches_on_end`.
  - `test_microphone_polls_and_closes_cleanly`: fake stream has `read_available` 480 then 0; `start` is called before the first read; `close()` calls `stop` then `close` once.
  - `test_no_input_device_message`: `query_devices` raises `ValueError` → `RuntimeError` with `Sound`.
  - `test_describe_does_not_raise_on_failed_preconditions`.
  - `test_backends.py::test_select_backend_by_platform` parametrized over `win32 → WindowsBackend`, `darwin → MacBackend`, `linux → LinuxBackend`, asserting `isinstance` (monkeypatching `sys.platform`; constructing a backend must not import any audio library).
- [ ] **Step 3: Run them.** Expected: FAIL.
- [ ] **Step 4: Implement** per Interfaces. Comment WHY for: dropping pre-start buffers, `stop()` outside the callback, the 2-channel cap, and that zeroed buffers (not errors) signal a missing permission.
- [ ] **Step 5: Run the full suite.** Expected: PASS.
- [ ] **Step 6: Commit** `feat: macOS core audio tap backend`.

---

### Task 9: Documentation and spec amendments

**Files:**
- Modify: `SPEC.md`, `README.md`, `docs/superpowers/specs/2026-11-07-cross-platform-capture-design.md`

- [ ] **Step 1: Apply the seven "Spec amendments" above to the design spec** (hardening list, Linux section, Source/PolledSource section, macOS section, silence-warning plumbing). Keep the open questions section; results are added in Task 10.
- [ ] **Step 2: Rewrite `SPEC.md`** (it is also the package readme) for three backends: the capture table, layout including `backends/`, dependencies per OS, and the `soundcard` mentions removed.
- [ ] **Step 3: Fill `README.md`** with per-OS setup: macOS (14.2+, Python 3.11+ via `uv venv --python 3.12`, grant the terminal *System Audio Recording* and *Microphone*, restart the terminal), Linux (`pulseaudio-utils`; PipeWire needs `pipewire-pulse`), Windows (unchanged); install with `pip install -e .`; troubleshooting for silent call audio.
- [ ] **Step 4: Verify** `grep -rn "soundcard" SPEC.md README.md` finds nothing, and the suite still passes.
- [ ] **Step 5: Commit** `docs: per-OS setup and updated spec`.

---

### Task 10: Live verification and hand-off

**Files:**
- Create: `scripts/e2e_linux.sh` (only if Docker is available)
- Modify: `docs/superpowers/specs/2026-11-07-cross-platform-capture-design.md` (record results)

- [ ] **Step 1: Floor check.** `uv venv --python /usr/bin/python3 .venv39` (3.9.6), `uv pip install --python .venv39/bin/python -e ".[dev]"`, run `.venv39/bin/pytest -q`. Expected: PASS (no `catap` installed there; proves the 3.9 floor). Then `.venv39/bin/recorder start` must fail with a message containing `Python 3.11` in the printed log tail (Review Focus 4).
- [ ] **Step 2: macOS permissions (needs the user).** Ask the user to add their terminal app under System Settings > Privacy & Security > *System Audio Recording Only* and *Microphone*, then restart the terminal. Run `.venv/bin/recorder devices`; expected: names the tap and the microphone, no `UNAVAILABLE`.
- [ ] **Step 3: Detached smoke test from the real path** (`/Volumes/External SSD/recorder`, Review Focus 5). Run `.venv/bin/recorder start`, then `afplay /System/Library/Sounds/Funk.aiff` in a loop for ~8 s while the user speaks, then `.venv/bin/recorder stop`. Expected: `Saved .../audio/recording-*.mp3`, no `Warning:` line. Verify audibility of both: `ffmpeg -i <mp3> -af volumedetect -f null -` reports `mean_volume` above −60 dB, and `.venv/bin/python scripts/diag.py` (foreground) prints loopback and mic RMS > 0.001. If `stop` prints the silent-call warning while `diag.py` hears the loopback, the permission is not following the detached daemon: **stop and amend the spec** (open question 1).
- [ ] **Step 4: Output-device change (needs the user, optional).** While recording, connect AirPods or switch the output device; record whether capture survives; write the finding into the spec's open question 2 and the README limits.
- [ ] **Step 5: Linux end to end.** If `docker info` succeeds, write `scripts/e2e_linux.sh` to build an `ubuntu:24.04` container with `pulseaudio pulseaudio-utils python3-venv ffmpeg`, start PulseAudio, load a null sink `virt` (default sink) and a `module-virtual-source` named `fakemic` (default source, not a monitor) fed from a second null sink, play 880 Hz to `virt` and 440 Hz into the mic sink for ~6 s while `recorder start/stop` runs, decode the mp3 to f32 PCM and assert via numpy FFT that both tones are present. Expected: both peaks above a noise threshold. If Docker is unavailable, state that in the README and the final report instead.
- [ ] **Step 6: Windows hand-off.** Windows cannot be run here. Prepare a checklist for the user: `pip install -e .`, `recorder devices`, `recorder start`/`stop` during a call, confirm identical behaviour to before.
- [ ] **Step 7: Record results** in the spec's open questions (answered or still open), run the full suite once more, and commit `docs: record live verification results`.
