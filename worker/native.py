"""Existing PCM padding and sender options; transport lives in pyatv_transport."""
from pathlib import Path


def playback_wait(at_unix_ms: int, duration: float, now: float) -> float:
    """Wait for the acknowledged audible anchor, not an assumed buffering delay."""
    return max(0, at_unix_ms / 1000 - now) + duration + 0.75


def append_silence(pcm: bytes, seconds: float = 3.0) -> bytes:
    """Do not tear down a receiver while its render buffer still holds speech."""
    if not 0 <= seconds <= 5 or len(pcm) % 4:
        raise ValueError('Invalid PCM size or trailing silence')
    return pcm + bytes(round(44100 * seconds) * 4)


def prepend_silence(pcm: bytes, seconds: float = .7) -> bytes:
    """Allow a receiver's output gain to settle before the first speech sample."""
    if not 0 <= seconds <= 2 or len(pcm) % 4:
        raise ValueError('Invalid PCM size or pre-roll')
    return bytes(round(44100 * seconds) * 4) + pcm


def start_timing(at_unix_ms: int, lead_seconds: float, now: float):
    """Scheduled timing, not microphone-measured acoustic latency."""
    return {'receiverBufferMs': round(max(0, at_unix_ms / 1000 - now) * 1000, 2),
            'preRollMs': round(lead_seconds * 1000, 2)}


class NativeSender:
    def __init__(self, root: Path, local_address: str, *, startup_timeout=22, tail_seconds=3.0, lead_seconds=.7):
        self.root, self.local_address = root, local_address
        self.startup_timeout, self.tail_seconds, self.lead_seconds = startup_timeout, tail_seconds, lead_seconds
