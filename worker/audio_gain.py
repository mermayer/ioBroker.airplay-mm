"""Playback-only attenuation. Cached speech and receiver volume stay untouched."""
from array import array
import asyncio
from contextlib import contextmanager, asynccontextmanager
import math
import os
from pathlib import Path
import sys
import tempfile
import wave


def validate_gain(level):
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) or not 0 <= level <= 100:
        raise ValueError('Master gain must be between 0 and 100 percent')
    return level


def attenuate_pcm16(pcm, level):
    level = validate_gain(level)
    if len(pcm) % 2:
        raise ValueError('Unvollstaendige PCM-Samples')
    if level == 100:
        return pcm
    if level == 0:
        return bytes(len(pcm))
    samples = array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    factor = level / 100
    scaled = array('h', (int(sample * factor) for sample in samples))
    if sys.byteorder != 'little':
        scaled.byteswap()
    return scaled.tobytes()


@contextmanager
def playback_wave(source, root, level):
    """Legacy file streaming needs a private, short-lived attenuated copy."""
    level = validate_gain(level)
    if level == 100:
        yield source
        return
    with wave.open(str(source), 'rb') as original:
        if original.getcomptype() != 'NONE' or original.getsampwidth() != 2 or original.getnchannels() not in (1, 2):
            raise ValueError('The speech file uses an unsupported PCM format')
        params = original.getparams()
        if not 0 < original.getnframes() / original.getframerate() <= 60:
            raise ValueError('Invalid speech file length')
        pcm = original.readframes(original.getnframes())
        if len(pcm) != params.nframes * params.nchannels * 2:
            raise ValueError('Incomplete speech file')
    folder = Path(root) / 'playback-audio'
    if folder.is_symlink():
        raise ValueError('Unexpected link in playback directory')
    folder.mkdir(mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='announcement-', suffix='.wav', dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            with wave.open(stream, 'wb') as audio:
                audio.setparams(params)
                audio.writeframes(attenuate_pcm16(pcm, level))
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


@asynccontextmanager
async def async_playback_wave(source, root, level):
    """Keep PCM conversion/file IO away from the active AirPlay event loop."""
    manager = playback_wave(source, root, level)
    if validate_gain(level) == 100:
        with manager as playback:
            yield playback
        return
    preparing = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        playback = await asyncio.shield(preparing)
    except asyncio.CancelledError:
        # A thread cannot be cancelled. Wait for its own file and clean it even
        # when cancellation arrives before preparation has yielded that file.
        try:
            await preparing
        except Exception:
            pass  # The synchronous helper already cleans a failed preparation.
        else:
            await asyncio.to_thread(manager.__exit__, None, None, None)
        raise
    try:
        yield playback
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)
