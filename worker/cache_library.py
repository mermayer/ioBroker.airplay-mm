"""Local audio catalog. Never accepts a file path from an API caller."""
import hashlib
import json
from pathlib import Path
import re
import time
import math
import wave
import logging

LOG = logging.getLogger('airplay-mm')

KEY = re.compile(r'^[a-f0-9]{64}$')


class CacheLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.index_file = self.root / 'cache-index.json'
        self.index = {}
        if self.index_file.is_file() and not self.index_file.is_symlink() and self.index_file.stat().st_size <= 4_000_000:
            try:
                value = json.loads(self.index_file.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    self.index = {k: v for k, v in value.items() if KEY.fullmatch(k) and isinstance(v, dict)}
            except (OSError, ValueError):
                pass  # Audio and its old history remain intact if the optional catalog is damaged.
        self.recover_history()

    def save(self):
        self.index = {key: value for key, value in self.index.items() if any((self.root / folder / (key + '.wav')).is_file() for folder in ('cache', 'speechify-cache'))}
        temporary = self.index_file.with_suffix('.tmp')
        if temporary.is_symlink() or self.index_file.is_symlink():
            raise ValueError('Unexpected link in cache directory')
        temporary.write_text(json.dumps(self.index, ensure_ascii=False), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.index_file)

    def recover_history(self):
        path = self.root / 'history.json'
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
            return
        try:
            rows = json.loads(path.read_text(encoding='utf-8'))
            from voices import VOICES, cache_key
            for row in rows if isinstance(rows, list) else []:
                text = row.get('text') if isinstance(row, dict) else None
                if not isinstance(text, str) or not 0 < len(text) <= 500:
                    continue
                variants = [(cache_key(text, voice), 'piper', spec['name']) for voice, spec in VOICES.items()]
                variants.append((hashlib.sha256(('sayit-google-v1:de:' + text).encode()).hexdigest(), 'sayit-google', 'SayIt · Google'))
                for key, provider, voice in variants:
                    if key not in self.index and (self.root / 'cache' / (key + '.wav')).is_file():
                        self.index[key] = {'text': text, 'provider': provider, 'voice': voice, 'created': row.get('created', 0)}
        except (OSError, ValueError, TypeError):
            pass

    def path(self, key):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError('Invalid cache ID')
        for folder in ('speechify-cache', 'cache'):
            base = self.root / folder
            path = base / (key + '.wav')
            if base.is_symlink() or path.is_symlink():
                raise ValueError('Unexpected link in cache directory')
            if path.is_file():
                return path
        raise ValueError('Cache entry no longer exists')

    def audio(self, key):
        path = self.path(key)
        with wave.open(str(path), 'rb') as audio:
            if audio.getcomptype() != 'NONE' or audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2):
                raise ValueError('Invalid audio format in cache')
            duration = audio.getnframes() / audio.getframerate()
            if not 0 < duration <= 60 or path.stat().st_size < audio.getnframes() * audio.getnchannels() * 2 + 44:
                raise ValueError('Incomplete audio file in cache')
        return path, duration

    def record(self, result, text, provider, voice, model=''):
        path = Path(result.path)
        if not KEY.fullmatch(path.stem):
            raise ValueError('Invalid cache ID')
        previous = self.index.get(path.stem, {})
        self.index[path.stem] = {'text': text, 'provider': provider, 'voice': voice, 'model': model,
                                'created': previous.get('created', time.time()), 'lastUsed': time.time(),
                                'durationSeconds': round(result.duration, 2)}
        try:
            self.save()
        except (OSError, ValueError) as error:
            LOG.warning('Cache description could not be saved; the completed audio file is preserved and playable: %s', str(error)[:500])

    def entries(self):
        entries = []
        for folder in ('cache', 'speechify-cache'):
            base = self.root / folder
            if base.is_symlink():
                continue
            for path in base.glob('*.wav'):
                if not KEY.fullmatch(path.stem) or path.is_symlink() or not path.is_file():
                    continue
                info = self.index.get(path.stem, {})
                used = info.get('lastUsed', path.stat().st_mtime)
                if not isinstance(used, (int, float)) or not math.isfinite(used):
                    used = path.stat().st_mtime
                entries.append({**info, 'key': path.stem, 'text': str(info.get('text', 'Older cache entry'))[:500],
                                'voice': str(info.get('voice', 'Previous voice'))[:80], 'bytes': path.stat().st_size,
                                'prepared': folder == 'speechify-cache', 'lastUsed': used})
        return sorted(entries, key=lambda row: row['lastUsed'], reverse=True)

    def listing(self, data=None):
        data = data or {}
        if not isinstance(data, dict) or set(data) - {'offset', 'limit'}:
            raise ValueError('Invalid cache query')
        offset, limit = data.get('offset', 0), data.get('limit', 50)
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 10000 or not 1 <= limit <= 50:
            raise ValueError('Invalid cache page range')
        rows = self.entries()
        return {'entries': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit,
                'bytes': sum(row['bytes'] for row in rows)}

    def delete(self, key):
        self.path(key).unlink()
        self.index.pop(key, None)
        try:
            self.save()
        except (OSError, ValueError) as error:
            LOG.warning('Cache entry deleted, but the description could not be saved: %s', str(error)[:500])
        return {'deleted': True, 'key': key}
