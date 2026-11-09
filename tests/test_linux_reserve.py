"""Replays of ``parec`` arrival orders through the real mixing path, proving the loopback reserve.

The engine is clocked by the microphone: at each mic block it takes whatever loopback frames
are buffered and ``fit_and_mix`` pads the shortfall with silence, so a burst that arrives a
moment late inserts silence into the call and shifts everything after it. These tests feed
scripted reads to two ``ParecSource`` readers over fake ``parec`` processes, consume them with
real ``DeviceStream`` buffers and ``fit_and_mix`` the way ``engine.record`` does, and measure
the silence inside the call audio. No real timing is involved: every arrival is handled by its
reader before the next one is fed.
"""

from __future__ import annotations

import threading
from typing import Iterator, List, Sequence, Tuple

import numpy as np
import pytest

from fake_parec import FakeProc, FakeSpawn
from recorder.audio_io import DeviceStream
from recorder.backends import linux
from recorder.backends.linux import ParecSource
from recorder.config import BLOCK, SAMPLERATE
from recorder.mix import fit_and_mix

WAIT = 2.0
# Call frame i carries (i + 1) * 2**-21: exact in float32, never zero, and decodable back to its
# capture index after it is summed with a silent microphone.
SCALE = 2.0 ** -21
MAX_SILENCE_INSIDE_CALL = 2
PIPE_READ_FRAMES = 65536 // 8

Script = Sequence[Tuple[str, int]]


@pytest.fixture(autouse=True)
def no_leaked_reader_threads() -> Iterator[None]:
    """Fail a test that leaves a reader thread behind, since it would outlive the suite."""
    yield
    leaked = [t for t in threading.enumerate() if t.name.startswith("parec-")]
    for thread in leaked:
        thread.join(WAIT)
    assert not [t for t in leaked if t.is_alive()]


class Replay:
    """A loopback and a microphone ``ParecSource`` feeding the engine's mic-clocked mixing step.

    Attributes:
        mixed: Every mixed block, in recording order.
        log: Lines the two streams logged; a clean replay logs nothing.
    """

    def __init__(self, reserve_frames: int) -> None:
        """Start both streams; the loopback holds back ``reserve_frames`` before its first delivery."""
        self.mixed: List[np.ndarray] = []
        self.log: List[str] = []
        self._loop_proc = FakeProc()
        self._mic_proc = FakeProc()
        stop = threading.Event()
        loopback = ParecSource("sink.monitor", spawn=FakeSpawn(self._loop_proc),
                               reserve_frames=reserve_frames)
        microphone = ParecSource("mic", spawn=FakeSpawn(self._mic_proc))
        self._loop = DeviceStream(loopback, SAMPLERATE, stop, self.log.append, "loopback")
        self._mic = DeviceStream(microphone, SAMPLERATE, stop, self.log.append, "mic")
        self._next_call_frame = 0
        self._mic_buffered = 0
        self._loop.start()
        self._mic.start()

    def arrive(self, stream: str, frames: int) -> None:
        """Deliver one pipe read of ``frames`` to ``"L"`` (call) or ``"M"`` (microphone).

        A microphone arrival runs the mixing step for every whole block it completes, as the
        engine's loop would the moment the mic data lands.
        """
        if stream == "L":
            index = np.arange(self._next_call_frame, self._next_call_frame + frames, dtype=np.float64)
            self._next_call_frame += frames
            values = ((index + 1) * SCALE).astype("<f4")
            self._loop_proc.stdout.feed(np.repeat(values[:, None], 2, axis=1).tobytes())
            assert self._loop_proc.stdout.wait_handled(WAIT)
            return
        self._mic_proc.stdout.feed(bytes(frames * 8))
        assert self._mic_proc.stdout.wait_handled(WAIT)
        self._mic_buffered += frames
        while self._mic_buffered >= BLOCK:
            # The mic frames are already buffered, so this returns what engine.record's
            # blocking pull would, without risking a hang if that ever regressed.
            mic_block = self._mic.pull_nowait(BLOCK)
            assert len(mic_block) == BLOCK
            loop_block = self._loop.pull_nowait(len(mic_block))
            self.mixed.append(fit_and_mix(loop_block, mic_block))
            self._mic_buffered -= BLOCK

    def close(self) -> None:
        """Stop both readers the way the engine does at the end of a recording."""
        try:
            self._loop.join()
        finally:
            self._mic.join()


def _replay(script: Script, reserve_frames: int) -> np.ndarray:
    replay = Replay(reserve_frames)
    try:
        for stream, frames in script:
            replay.arrive(stream, frames)
    finally:
        replay.close()
    assert replay.log == []
    return np.concatenate(replay.mixed)[:, 0]


def _call_index(left: np.ndarray) -> np.ndarray:
    """Return each mixed frame's call capture index, or -1 where the call side was padding."""
    return np.rint(left.astype(np.float64) / SCALE).astype(np.int64) - 1


def silences_inside_call(left: np.ndarray) -> List[int]:
    """Return the length of every run of padding between the first and last call frame.

    Raises:
        AssertionError: If call frames were dropped, duplicated or reordered, or none was mixed.
    """
    index = _call_index(left)
    present = np.flatnonzero(index >= 0)
    assert len(present), "no call audio was mixed"
    call = index[present]
    assert call[0] == 0 and np.all(np.diff(call) == 1), "call frames dropped or reordered"
    runs, run = [], 0
    for padded in index[present[0]:present[-1] + 1] < 0:
        if padded:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    return runs


L, M = "L", "M"
STEADY_LOOP_FIRST = [(L, BLOCK), (M, BLOCK)] * 2
STEADY_MIC_FIRST = [(M, BLOCK), (L, BLOCK)] * 2
TAIL = [(L, BLOCK), (M, BLOCK)] * 3

SCENARIOS = [
    pytest.param(
        STEADY_LOOP_FIRST + [(L, BLOCK), (M, BLOCK), (M, BLOCK), (L, BLOCK)] + TAIL,
        [BLOCK],
        id="i-loop-burst-right-after-its-mic-pull",
    ),
    pytest.param(
        STEADY_LOOP_FIRST + [(L, 3600), (M, BLOCK), (L, 1200)] + TAIL,
        [1200],
        id="ii-split-read-3600+1200",
    ),
    pytest.param(
        STEADY_LOOP_FIRST + [(L, 1920), (M, BLOCK), (L, 2880)] + TAIL,
        [2880],
        id="ii-split-read-1920+2880",
    ),
    # 250 ms is 12000 frames per stream, which a 64 KiB pipe read splits into 8192 + 3808.
    pytest.param(
        STEADY_LOOP_FIRST + [(L, PIPE_READ_FRAMES), (M, PIPE_READ_FRAMES), (M, 3808), (L, 3808)]
        + TAIL,
        [1408],
        id="iii-both-stall-250ms-LMML",
    ),
    pytest.param(
        STEADY_LOOP_FIRST + [(M, PIPE_READ_FRAMES), (L, PIPE_READ_FRAMES), (M, 3808), (L, 3808)]
        + TAIL,
        [BLOCK],
        id="iii-both-stall-250ms-MLML",
    ),
    pytest.param(
        STEADY_MIC_FIRST + [(M, 2 * BLOCK), (L, BLOCK), (L, BLOCK)] + TAIL,
        [BLOCK],
        id="iv-mic-backlog-of-two-blocks-first",
    ),
]


@pytest.mark.parametrize("script, _holes", SCENARIOS)
def test_loopback_reserve_leaves_no_silence_inside_the_call(script, _holes):
    """With the backend's reserve, late or split call bursts never pad the call with silence."""
    left = _replay(script, linux._LOOPBACK_RESERVE_FRAMES)
    assert max(silences_inside_call(left), default=0) <= MAX_SILENCE_INSIDE_CALL


@pytest.mark.parametrize("script, holes", SCENARIOS)
def test_without_a_reserve_the_same_arrivals_insert_silence(script, holes):
    """Control: the identical arrivals with no reserve put these holes into the call."""
    assert silences_inside_call(_replay(script, 0)) == holes


def _lags(left: np.ndarray) -> set:
    """Return every distinct offset between a call frame's mixed position and its capture index."""
    index = _call_index(left)
    present = np.flatnonzero(index >= 0)
    return set((present - index[present]).tolist())


def test_reserve_delays_the_call_by_a_constant():
    """The price of the reserve is a fixed lag of the call behind the microphone, not a drift."""
    script = STEADY_LOOP_FIRST + TAIL
    assert _lags(_replay(script, 0)) == {0}
    assert _lags(_replay(script, linux._LOOPBACK_RESERVE_FRAMES)) == {BLOCK}
