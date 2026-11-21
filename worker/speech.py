"""Bounded disk cache and isolated persistent Piper worker."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sys
import time
import wave
from voices import DEFAULT_VOICE, cache_key, voice_spec

LOG = logging.getLogger('airplay-mm')

@dataclass
class SpeechResult:
    path: Path
    duration: float
    cache_hit: bool
    generation_ms: float
    model_load_ms: float = 0
    provider: str = ''
    voice: str = ''
    fallback_reason: str = ''

    def __iter__(self):
        yield self.path
        yield self.duration

class AudioCache:
    def __init__(self, folder: Path, max_entries=256, max_bytes=64 * 1024 * 1024):
        self.folder = folder
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.hits = 0
        self.misses = 0

    def files(self):
        return [p for p in self.folder.glob('[0-9a-f]' * 64 + '.wav') if p.is_file() and not p.is_symlink()]

    def lookup(self, key: str):
        path = self.folder / (key + '.wav')
        if path.is_symlink():
            raise ValueError('Unexpected link in speech cache')
        if not path.is_file():
            return None
        try:
            with wave.open(str(path), 'rb') as audio:
                if audio.getcomptype() != 'NONE' or audio.getnchannels() not in (1, 2) or audio.getsampwidth() != 2:
                    raise ValueError('Invalid audio format')
                duration = audio.getnframes() / audio.getframerate()
                if not 0 < duration <= 60:
                    raise ValueError('Invalid audio duration')
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if path.stat().st_size < expected + 44:
                    raise ValueError('Incomplete audio file')
            path.touch()
            return path, duration
        except (OSError, EOFError, wave.Error, ValueError):
            LOG.warning('Regenerating corrupt audio file: %s', path.name[:16])
            path.unlink(missing_ok=True)
            return None

    def prune(self, keep: Path | None = None):
        files = sorted(self.files(), key=lambda p: p.stat().st_mtime, reverse=True)
        size = sum(p.stat().st_size for p in files)
        count = len(files)
        for path in reversed(files):
            if count <= self.max_entries and size <= self.max_bytes:
                break
            if path == keep:
                continue
            length = path.stat().st_size
            path.unlink(missing_ok=True)
            size -= length
            count -= 1

    def stats(self):
        try:
            files = self.files()
            entries, size = len(files), sum(p.stat().st_size for p in files)
        except OSError:
            entries, size = 0, 0
        return {'entries': entries, 'bytes': size,
                'maxEntries': self.max_entries, 'maxBytes': self.max_bytes, 'hits': self.hits, 'misses': self.misses}

class SpeechService:
    def __init__(self, root: Path, *, voice=DEFAULT_VOICE, timeout=30, launch=None):
        self.root = root
        self.voice = voice_spec(voice)
        self.cache = AudioCache(root / 'cache')
        self.timeout = timeout
        self.launch = launch or asyncio.create_subprocess_exec
        self.process = None
        self.ready = False
        self.closed = False
        self.sequence = 0
        self.model_load_ms = 0
        self.last_error = ''
        self.lock = asyncio.Lock()
        self.stderr_task = None

    async def _stderr(self, process):
        while line := await process.stderr.readline():
            LOG.debug('Speech process: %s', line.decode('utf-8', errors='replace')[:1000].strip())

    async def _receive(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), self.timeout)
        if not line:
            raise RuntimeError('Speech process ended unexpectedly')
        try:
            result = json.loads(line)
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError('Invalid speech process response') from error
        if not isinstance(result, dict):
            raise RuntimeError('Invalid speech process response')
        return result

    async def _ensure_ready(self):
        if self.closed:
            raise RuntimeError('Speech generation was stopped')
        if self.process and self.process.returncode is None and self.ready:
            return
        await self._stop()
        self.process = await self.launch(sys.executable, '-u', str(Path(__file__).with_name('speech_worker.py')), '--root', str(self.root), '--voice', self.voice['id'],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '1'}, limit=16384)
        self.stderr_task = asyncio.create_task(self._stderr(self.process))
        answer = await self._receive()
        if answer.get('event') != 'ready' or answer.get('voice') != self.voice['id']:
            raise RuntimeError('Voice could not be loaded: ' + str(answer.get('error', 'no confirmation'))[:800])
        self.ready = True
        self.model_load_ms = float(answer.get('modelLoadMs', 0))
        self.last_error = ''
        LOG.info('Local voice %s remains loaded; initial load time %.0f ms', self.voice['name'], self.model_load_ms)

    async def start(self):
        async with self.lock:
            try:
                self.cache.prune()
                await self._ensure_ready()
            except BaseException:
                await self._stop()
                raise

    async def synthesize(self, text: str):
        if not isinstance(text, str) or not text.strip() or len(text) > 500 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('Enter an announcement with 1 to 500 characters')
        async with self.lock:
            if self.closed:
                raise RuntimeError('Speech generation was stopped')
            start = time.perf_counter()
            key = cache_key(text, self.voice['id'])
            cached = self.cache.lookup(key)
            if cached:
                self.cache.hits += 1
                return SpeechResult(cached[0], cached[1], True, round((time.perf_counter() - start) * 1000, 2))
            self.cache.misses += 1
            try:
                await self._ensure_ready()
                self.sequence += 1
                identifier = str(self.sequence)
                self.process.stdin.write((json.dumps({'id': identifier, 'text': text}, ensure_ascii=True) + '\n').encode('utf-8'))
                await self.process.stdin.drain()
                result = await self._receive()
                if result.get('id') != identifier or not result.get('ok') or result.get('key') != key:
                    raise RuntimeError('Speech generation failed: ' + str(result.get('error', 'unpassende Antwort'))[:800])
                audio = self.cache.lookup(key)
                if not audio:
                    raise RuntimeError('Generated audio file is invalid')
                self.cache.prune(keep=audio[0])
                self.last_error = ''
                return SpeechResult(audio[0], audio[1], False, round((time.perf_counter() - start) * 1000, 2), self.model_load_ms)
            except BaseException as error:
                if not isinstance(error, asyncio.CancelledError):
                    self.last_error = type(error).__name__ + ': ' + str(error)[:800]
                await self._stop()
                (self.root / 'cache' / (key + '.part.wav')).unlink(missing_ok=True)
                (self.root / 'cache' / (key + '.wav')).unlink(missing_ok=True)
                raise

    async def _stop(self):
        process, self.process = self.process, None
        self.ready = False
        if process:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if self.stderr_task:
                self.stderr_task.cancel()
                await asyncio.gather(self.stderr_task, return_exceptions=True)
                self.stderr_task = None

    def stats(self):
        return {**self.cache.stats(), 'ready': self.ready and bool(self.process) and self.process.returncode is None,
                'provider': 'piper', 'voice': self.voice['name'] + ' · German · Local', 'voiceId': self.voice['id'], 'onlineForNewTexts': False,
                'modelLoadMs': self.model_load_ms, 'lastError': self.last_error}

    async def close(self):
        self.closed = True
        async with self.lock:
            await self._stop()
