"""A scripted stand-in for ``openai.OpenAI`` so transcription tests never touch the network."""

from types import SimpleNamespace


class _Response:
    """The one attribute of a transcription response that the code under test reads."""

    def __init__(self, text):
        self.text = text


class _Transcriptions:
    """Answers each ``create`` call with the next scripted text, or raises the next exception."""

    def __init__(self, script, calls):
        self._script = list(script)
        self._calls = calls

    def create(self, **kwargs):
        """Record the call, then return or raise the next scripted outcome."""
        self._calls.append(kwargs)
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)


class FakeClient:
    """Mimics ``client.audio.transcriptions.create(...)``.

    Attributes:
        calls: The keyword arguments of every ``create`` call, in order.
    """

    def __init__(self, script):
        self.calls = []
        self.audio = SimpleNamespace(transcriptions=_Transcriptions(script, self.calls))
