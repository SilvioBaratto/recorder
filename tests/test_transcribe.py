"""Tests for sending chunks to the transcription API and assembling the transcript."""

import pytest

from fake_openai import FakeClient
from recorder.chunker import Chunk
from recorder.transcribe import (
    TranscriptionError,
    join_texts,
    make_client,
    prompt_tail,
    transcribe_chunks,
)


def _chunks(count):
    """Return ``count`` contiguous 100 s chunks whose bytes identify them."""
    return [Chunk(start_s=i * 100.0, duration_s=100.0, mp3=f"mp3-{i}".encode()) for i in range(count)]


def test_chunks_sent_in_order_with_model():
    """Each chunk is uploaded once, in order, as a named mp3 to gpt-transcribe."""
    chunks = _chunks(3)
    client = FakeClient(["a", "b", "c"])
    texts = transcribe_chunks(chunks, client)
    assert texts == ["a", "b", "c"]
    assert [call["model"] for call in client.calls] == ["gpt-transcribe"] * 3
    assert [call["file"] for call in client.calls] == [
        (f"chunk-00{i}.mp3", chunk.mp3, "audio/mpeg") for i, chunk in enumerate(chunks)
    ]


def test_first_call_has_no_prompt_later_calls_carry_tail():
    """Context flows forward: the first chunk has none, the next gets the text so far."""
    client = FakeClient(["first text", "second"])
    transcribe_chunks(_chunks(2), client)
    assert "prompt" not in client.calls[0]
    assert client.calls[1]["prompt"] == prompt_tail("first text")


@pytest.mark.parametrize(
    "text, limit, expected",
    [
        ("alpha beta gamma delta", 12, "gamma delta"),
        ("alpha beta gamma delta", 10, "delta"),
        ("short", 400, "short"),
        ("", 400, ""),
        ("abcdef", 3, ""),
    ],
)
def test_prompt_tail_trims_to_word_boundary(text, limit, expected):
    """The tail never starts in the middle of a word."""
    assert prompt_tail(text, limit) == expected


def test_languages_and_keywords_only_when_given():
    """Hints are sent through extra_body only when the caller supplied them."""
    bare = FakeClient(["x"])
    transcribe_chunks(_chunks(1), bare)
    assert "extra_body" not in bare.calls[0]

    both = FakeClient(["x"])
    transcribe_chunks(_chunks(1), both, languages=["it"], keywords=["AC-42"])
    assert both.calls[0]["extra_body"] == {"languages": ["it"], "keywords": ["AC-42"]}

    only_languages = FakeClient(["x"])
    transcribe_chunks(_chunks(1), only_languages, languages=["it", "en"])
    assert only_languages.calls[0]["extra_body"] == {"languages": ["it", "en"]}
    assert all("language" not in call for call in both.calls + only_languages.calls)


def test_empty_text_dropped_and_prompt_keeps_last_text():
    """A silent chunk adds nothing, and the next chunk still gets the last real text."""
    client = FakeClient(["hello world", "", "next"])
    texts = transcribe_chunks(_chunks(3), client)
    assert texts == ["hello world", "", "next"]
    assert client.calls[2]["prompt"] == "hello world"
    assert join_texts(["perché", "", "così"]) == "perché\n\ncosì"


def test_failure_raises_with_completed_texts():
    """A failing chunk reports where it was and keeps what was already transcribed."""
    boom = RuntimeError("boom")
    client = FakeClient(["a", boom])
    with pytest.raises(TranscriptionError) as caught:
        transcribe_chunks(_chunks(3), client)
    error = caught.value
    assert error.chunk_index == 1
    assert error.completed == ["a"]
    assert error.start_s == 100.0
    assert error.cause is boom
    assert error.__cause__ is boom


def test_error_from_the_chunk_iterator_is_wrapped():
    """A decode failure mid-recording keeps the transcript gathered so far, too."""

    def chunks():
        """Yield one good chunk, then fail the way a broken decode would."""
        yield _chunks(1)[0]
        raise ValueError("decode failed")

    with pytest.raises(TranscriptionError) as caught:
        transcribe_chunks(chunks(), FakeClient(["a"]))
    assert caught.value.chunk_index == 1
    assert caught.value.completed == ["a"]
    assert caught.value.start_s == 100.0
    assert isinstance(caught.value.cause, ValueError)


def test_keyboard_interrupt_is_wrapped():
    """Ctrl-C mid-upload still hands back the finished chunks."""
    with pytest.raises(TranscriptionError) as caught:
        transcribe_chunks(_chunks(2), FakeClient(["a", KeyboardInterrupt()]))
    assert isinstance(caught.value.cause, KeyboardInterrupt)
    assert caught.value.completed == ["a"]


def test_progress_callback():
    """Progress is reported after each chunk with the audio covered so far."""
    seen = []
    transcribe_chunks(_chunks(2), FakeClient(["a", "b"]), on_progress=lambda n, s: seen.append((n, s)))
    assert seen == [(1, 100.0), (2, 200.0)]


def test_make_client_retries_and_timeout(monkeypatch):
    """Long uploads get patient retries and a generous timeout."""
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = make_client()
    assert client.max_retries == 5
    assert client.timeout == 600


OPENING = "Buongiorno a tutti, iniziamo la riunione di oggi con il primo punto all'ordine del giorno."
FULL_TEXT = OPENING + " Il secondo punto riguarda il budget del team e le assunzioni previste."


def test_prompt_echoed_on_a_silent_chunk_is_dropped():
    """With no speech to transcribe the model repeats its prompt; that must not enter the transcript."""
    client = FakeClient([OPENING, OPENING, OPENING])
    texts = transcribe_chunks(_chunks(3), client)
    assert texts == [OPENING, "", ""]
    assert client.calls[2]["prompt"] == OPENING


def test_partial_echo_of_the_prompt_is_dropped():
    """A truncated repeat of the prompt is still a repeat."""
    client = FakeClient([FULL_TEXT, "Il secondo punto riguarda il budget del team"])
    assert transcribe_chunks(_chunks(2), client) == [FULL_TEXT, ""]


def test_short_real_text_that_appears_in_the_prompt_is_kept():
    """A genuine short utterance is not mistaken for an echo just because the prompt has the word."""
    client = FakeClient([OPENING, "riunione"])
    assert transcribe_chunks(_chunks(2), client) == [OPENING, "riunione"]
