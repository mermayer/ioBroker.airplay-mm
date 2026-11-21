"""Allowlisted local voices and stable, voice-specific cache keys."""
import hashlib

DEFAULT_VOICE = 'de_DE-thorsten-medium'
VOICES = {
    DEFAULT_VOICE: {'id': DEFAULT_VOICE, 'name': 'Thorsten', 'model': 'voices/de_DE-thorsten-medium.onnx',
                    'cachePrefix': 'piper-1.8.0:thorsten-medium:'},
    'de_DE-ramona-low': {'id': 'de_DE-ramona-low', 'name': 'Ramona', 'model': 'voices/ramona/de_DE-ramona-low.onnx',
                         'cachePrefix': 'piper-1.8.0:ramona-low:'},
}

def voice_spec(identifier=DEFAULT_VOICE):
    if not isinstance(identifier, str) or identifier not in VOICES:
        raise ValueError('Unknown local voice')
    return VOICES[identifier]

def cache_key(text, voice=DEFAULT_VOICE):
    return hashlib.sha256((voice_spec(voice)['cachePrefix'] + text).encode('utf-8')).hexdigest()
