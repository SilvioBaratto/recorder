"""Send audio chunks to OpenAI one after another and assemble the transcript.

Knows nothing about decoding audio. The ``openai`` package comes from the optional
``transcribe`` extra and is imported inside ``make_client`` only.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, List, Optional, Sequence

from .chunker import Chunk
from .config import PROMPT_TAIL_CHARS, TRANSCRIBE_MODEL

_CLIENT_RETRIES = 5
_CLIENT_TIMEOUT_SECONDS = 600

_MIN_ECHO_CHARS = 40
"""Shortest verbatim repeat of the prompt that is treated as an echo; shorter ones may be speech."""


class TranscriptionError(Exception):
    """A chunk could not be obtained or transcribed; carries what was finished before it.

    Attributes:
        chunk_index: Zero-based index of the chunk that failed.
        start_s: Offset in the recording, in seconds, where the failed step began.
        completed: Transcript text of every chunk finished before the failure.
        cause: The original exception (``KeyboardInterrupt`` when the user stopped the run).
    """

    def __init__(
        self, chunk_index: int, start_s: float, completed: List[str], cause: BaseException
    ) -> None:
        super().__init__(f"chunk {chunk_index + 1} (from {start_s:.0f} s) failed: {cause}")
        self.chunk_index = chunk_index
        self.start_s = start_s
        self.completed = completed
        self.cause = cause


def prompt_tail(text: str, limit: int = PROMPT_TAIL_CHARS) -> str:
    """Return the last ``limit`` characters of ``text``, never starting mid-word.

    Args:
        text: Transcript so far.
        limit: Maximum number of characters to keep.

    Returns:
        The tail with surrounding whitespace removed, or an empty string for empty input.
    """
    if len(text) <= limit:
        return text.strip()
    tail = text[-limit:]
    if not text[-limit - 1].isspace() and not tail[0].isspace():
        parts = re.split(r"\s+", tail, maxsplit=1)
        tail = parts[1] if len(parts) > 1 else ""
    return tail.strip()


def _is_prompt_echo(text: str, prompt: str) -> bool:
    """Return whether ``text`` only repeats ``prompt``.

    Given a prompt, ``gpt-transcribe`` answers a chunk that holds no speech (silence, hiss, room
    noise) with the prompt itself instead of an empty text. Left alone, a recording that runs on
    after the meeting would end with the last paragraph repeated once per silent chunk. Spoken
    words that reproduce 40 or more characters of the previous text verbatim are not plausible,
    so a verbatim repeat is treated as an echo.
    """
    said = " ".join(text.split())
    given = " ".join(prompt.split())
    return bool(said) and said in given and len(said) >= min(len(given), _MIN_ECHO_CHARS)


def join_texts(texts: Sequence[str]) -> str:
    """Join chunk transcripts with a blank line, skipping chunks that held no speech."""
    return "\n\n".join(text for text in texts if text)


def transcribe_chunks(
    chunks: Iterable[Chunk],
    client: Any,
    *,
    languages: Sequence[str] = (),
    keywords: Sequence[str] = (),
    on_progress: Optional[Callable[[int, float], None]] = None,
) -> List[str]:
    """Transcribe ``chunks`` in order and return one stripped text per chunk.

    Chunks go out one at a time because each request's ``prompt`` is the tail of the transcript
    so far, which keeps names and sentences consistent across the cuts. A chunk without speech
    yields an empty string, including when the model answers it by repeating the prompt, and does
    not erase that context.

    Args:
        chunks: Chunks in recording order; may be a lazy iterator.
        client: An ``openai.OpenAI``-compatible client.
        languages: ISO language hints; sent only when non-empty. Never sent as ``language``.
        keywords: Single-line terms likely to be spoken; sent only when non-empty.
        on_progress: Called after each chunk as ``(chunks_done, audio_seconds_done)``.

    Returns:
        The stripped text of every chunk, in order, empty strings included.

    Raises:
        TranscriptionError: On any failure while reading a chunk or transcribing it, including
            ``KeyboardInterrupt``. It carries the texts finished so far.
    """
    texts: List[str] = []
    audio_done = 0.0
    iterator = iter(chunks)
    while True:
        index = len(texts)
        try:
            chunk = next(iterator)
        except StopIteration:
            return texts
        except (Exception, KeyboardInterrupt) as exc:
            raise TranscriptionError(index, audio_done, list(texts), exc) from exc

        request: dict = {
            "model": TRANSCRIBE_MODEL,
            "file": (f"chunk-{index:03d}.mp3", chunk.mp3, "audio/mpeg"),
        }
        context = prompt_tail(join_texts(texts))
        if context:
            request["prompt"] = context
        hints: dict = {}
        if languages:
            hints["languages"] = list(languages)
        if keywords:
            hints["keywords"] = list(keywords)
        if hints:
            request["extra_body"] = hints

        try:
            response = client.audio.transcriptions.create(**request)
        except (Exception, KeyboardInterrupt) as exc:
            raise TranscriptionError(index, chunk.start_s, list(texts), exc) from exc
        text = (response.text or "").strip()
        texts.append("" if _is_prompt_echo(text, request.get("prompt", "")) else text)
        audio_done = chunk.start_s + chunk.duration_s
        if on_progress:
            on_progress(len(texts), audio_done)


def make_client() -> Any:
    """Return an OpenAI client for long uploads.

    The key comes from ``OPENAI_API_KEY`` in the environment; the SDK retries rate limits,
    server errors and dropped connections on its own.
    """
    from openai import OpenAI

    return OpenAI(max_retries=_CLIENT_RETRIES, timeout=_CLIENT_TIMEOUT_SECONDS)
