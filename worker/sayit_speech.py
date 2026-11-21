"""Actual SayIt Google generation in an isolated helper; local WAV cache."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import wave

import miniaudio

from speech import AudioCache, SpeechResult

LOG = logging.getLogger('airplay-mm')

def cache_key(text):
    return hashlib.sha256(('sayit-google-v1:de:' + text).encode('utf-8')).hexdigest()

def decode_chunks(chunks):
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 32:
        raise ValueError('Invalid number of SayIt speech segments')
    pcm = bytearray()
    total = 0
    for number, encoded in enumerate(chunks):
        if not isinstance(encoded, str) or len(encoded) > 1_400_000:
            raise ValueError('Invalid SayIt audio data')
        data = base64.b64decode(encoded, validate=True)
        total += len(data)
        if len(data) < 100 or total > 2 * 1024 * 1024:
            raise ValueError('Invalid SayIt audio size')
        decoded = miniaudio.decode(data, nchannels=1, sample_rate=24000, output_format=miniaudio.SampleFormat.SIGNED16)
        if decoded.sample_rate != 24000 or decoded.nchannels != 1 or not decoded.samples:
            raise ValueError('Invalid SayIt audio format')
        if number:
            pcm.extend(bytes(3840))  # 80 ms between the independently generated parts.
        pcm.extend(decoded.samples.tobytes())
        if len(pcm) > 24000 * 2 * 60:
            raise ValueError('Announcement is longer than 60 seconds')
    if len(pcm) < 2:
        raise ValueError('SayIt did not generate an audible audio file')
    return bytes(pcm)

class SayItSpeechService:
    def __init__(self, root: Path, *, timeout=30, launch=None):
        self.root = root
        self.cache = AudioCache(root / 'cache')
        self.timeout = timeout
        self.launch = launch or asyncio.create_subprocess_exec
        self.lock = asyncio.Lock()
        self.closed = False
        self.ready = False
        self.process = None
        self.last_error = ''
        self.version = ''
        self.last_parts = 0

    async def _read(self, stream, limit):
        output = bytearray()
        while data := await stream.read(16384):
            output.extend(data)
            if len(output) > limit:
                raise RuntimeError('SayIt response exceeds size limit')
        return bytes(output)

    async def _stop_process(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
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

    async def _call(self, text=None):
        helper = Path(__file__).resolve().parent.parent / 'lib' / 'sayit-source.js'
        args = ['--check'] if text is None else ['--root', str(self.root)]
        process = await self.launch(os.environ.get('AIRPLAY_NODE_BINARY', 'node'), str(helper), *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy())
        self.process = process
        tasks = []
        try:
            if text is not None:
                process.stdin.write(json.dumps({'text': text}, ensure_ascii=True).encode('utf-8'))
                await process.stdin.drain()
            process.stdin.close()
            tasks = [asyncio.create_task(self._read(process.stdout, 3_000_000)),
                     asyncio.create_task(self._read(process.stderr, 16384)), asyncio.create_task(process.wait())]
            out, _, code = await asyncio.wait_for(asyncio.gather(*tasks), self.timeout)
            result = json.loads(out)
            if not isinstance(result, dict) or code or not result.get('ok'):
                raise RuntimeError(str(result.get('error', 'SayIt speech generation failed'))[:600] if isinstance(result, dict) else 'Invalid SayIt response')
            if result.get('provider') != 'sayit-google' or result.get('language') != 'de':
                raise RuntimeError('SayIt returned an unexpected speech provider')
            self.version = str(result.get('sayitVersion', ''))[:50]
            self.ready = True
            return result
        finally:
            await self._stop_process()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def start(self):
        async with self.lock:
            self.cache.prune()
            try:
                await self._call()
                LOG.info('SayIt %s ready as speech provider: Google German; new texts are generated online', self.version)
            except Exception as error:
                self.last_error = str(error)[:800]
                self.ready = False
                raise

    async def synthesize(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 500 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('Enter an announcement with 1 to 500 characters')
        async with self.lock:
            if self.closed:
                raise RuntimeError('Speech generation was stopped')
            began = time.perf_counter()
            key = cache_key(text)
            cached = self.cache.lookup(key)
            if cached:
                self.cache.hits += 1
                return SpeechResult(cached[0], cached[1], True, round((time.perf_counter() - began) * 1000, 2))
            self.cache.misses += 1
            temporary = self.root / 'cache' / (key + '.part.wav')
            output = self.root / 'cache' / (key + '.wav')
            try:
                result = await self._call(text)
                if result.get('key') != key:
                    raise RuntimeError('SayIt response belongs to a different text')
                pcm = await asyncio.to_thread(decode_chunks, result.get('chunks'))
                self.last_parts = len(result['chunks'])
                if temporary.is_symlink() or output.is_symlink():
                    raise ValueError('Unexpected link in speech cache')
                with wave.open(str(temporary), 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(pcm)
                temporary.chmod(0o600)
                temporary.replace(output)
                audio = self.cache.lookup(key)
                if not audio:
                    raise RuntimeError('Generated SayIt audio file is invalid')
                self.cache.prune(keep=output)
                self.last_error = ''
                return SpeechResult(audio[0], audio[1], False, round((time.perf_counter() - began) * 1000, 2))
            except BaseException as error:
                if not isinstance(error, asyncio.CancelledError):
                    self.last_error = type(error).__name__ + ': ' + str(error)[:800]
                raise
            finally:
                temporary.unlink(missing_ok=True)
                (self.root / 'cache' / (key + '.sayit.part.mp3')).unlink(missing_ok=True)

    def stats(self):
        return {**self.cache.stats(), 'ready': self.ready, 'provider': 'sayit-google', 'voice': 'SayIt · Google German',
                'onlineForNewTexts': True, 'sayitVersion': self.version, 'lastParts': self.last_parts, 'lastError': self.last_error}

    async def close(self):
        self.closed = True
        self.ready = False
        await self._stop_process()
