"""Capture constants shared by the CLI and the daemon.

Kept import-light (no audio libraries) so `recorder status`/`stop` stay fast and testable.
"""

SAMPLERATE = 48000
"""Capture/encode sample rate in Hz. 48 kHz matches typical WASAPI device defaults."""

BLOCK = 4800
"""Frames per capture block (~0.1 s at 48 kHz). The unit of block-for-block stream pairing."""

BITRATE = 128
"""mp3 bitrate in kbps. 128 CBR stereo is ample for speech."""
