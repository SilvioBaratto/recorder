"""Capture constants shared by the CLI and the daemon.

Kept import-light (no audio libraries) so `recorder status`/`stop` stay fast and testable.
"""

SAMPLERATE = 48000
"""Capture/encode sample rate in Hz. 48 kHz matches typical WASAPI device defaults."""

BLOCK = 4800
"""Frames per capture block (~0.1 s at 48 kHz). The unit of block-for-block stream pairing."""

BITRATE = 128
"""mp3 bitrate in kbps. 128 CBR stereo is ample for speech."""

TRANSCRIBE_MODEL = "gpt-transcribe"
"""OpenAI model used by ``recorder transcribe`` for completed audio files."""

STT_SAMPLERATE = 16000
"""Sample rate in Hz of the mono chunks uploaded for transcription."""

STT_BITRATE = 48
"""mp3 bitrate in kbps of an uploaded chunk; about 7 MB per 20 minutes."""

CHUNK_TARGET_SECONDS = 1200
"""Audio length after which the chunker starts looking for a place to cut."""

CHUNK_SEARCH_SECONDS = 60
"""How far past the target the chunker looks for the quietest moment to cut."""

PROMPT_TAIL_CHARS = 400
"""Characters of the transcript so far passed as ``prompt`` to give the next chunk context."""

UPLOAD_LIMIT_BYTES = 24_000_000
"""Largest chunk the chunker will produce; the API rejects files over 25 MB."""
