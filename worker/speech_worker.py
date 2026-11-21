"""Single-purpose, persistent Piper process. Loaded once; no network or playback."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import wave
from voices import DEFAULT_VOICE, cache_key, voice_spec

def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)

def main(root: Path, voice=DEFAULT_VOICE):
    selected = voice_spec(voice)
    if root.resolve() != root or root.is_symlink():
        raise ValueError('Invalid voice directory')
    cache = root / 'cache'
    if cache.is_symlink() or not cache.is_dir():
        raise ValueError('Invalid cache directory')
    start = time.perf_counter()
    from piper import PiperVoice
    model = PiperVoice.load(str(root / selected['model']))
    emit({'event': 'ready', 'voice': selected['id'], 'modelLoadMs': round((time.perf_counter() - start) * 1000, 2)})
    for line in sys.stdin:
        message = {}
        temporary = None
        try:
            if len(line) > 16000:
                raise ValueError('Speech request is too large')
            message = json.loads(line)
            if not isinstance(message, dict) or set(message) != {'id', 'text'}:
                raise ValueError('Invalid speech request')
            text = message['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 500 or any(ord(c) < 32 and c not in '\n\t' for c in text):
                raise ValueError('Invalid announcement text')
            key = cache_key(text, selected['id'])
            output = cache / (key + '.wav')
            temporary = cache / (key + '.part.wav')
            if output.is_symlink() or temporary.is_symlink():
                raise ValueError('Unexpected cache link')
            start = time.perf_counter()
            with wave.open(str(temporary), 'wb') as target:
                model.synthesize_wav(text, target)
            with wave.open(str(temporary), 'rb') as audio:
                duration = audio.getnframes() / audio.getframerate()
            if not 0 < duration <= 60:
                raise ValueError('Announcement is empty or longer than 60 seconds')
            temporary.chmod(0o600)
            temporary.replace(output)
            emit({'id': message['id'], 'ok': True, 'key': key, 'duration': duration,
                  'synthesisMs': round((time.perf_counter() - start) * 1000, 2)})
        except Exception as error:
            emit({'id': message.get('id') if isinstance(message, dict) else None, 'ok': False,
                  'error': type(error).__name__ + ': ' + str(error)[:800]})
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--voice', default=DEFAULT_VOICE)
    try:
        args = parser.parse_args()
        main(Path(args.root).absolute(), args.voice)
    except Exception as error:
        emit({'event': 'fatal', 'error': type(error).__name__ + ': ' + str(error)[:1000]})
        sys.exit(1)
