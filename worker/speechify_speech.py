"""Optional Speechify synthesis; existing local audio never requires the API."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import unicodedata
import logging
import wave
import struct
from speech import AudioCache, SpeechResult


async def synthesize_with_fallback(service, local, text):
    try:
        return await service.synthesize(text)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        reason = str(error)[:500]
        logging.getLogger('airplay-mm').warning('Speechify: %s. Using local Ramona fallback.', reason)
        result = await local.synthesize(text)
        result.provider, result.voice, result.fallback_reason = 'piper', 'Ramona', reason
        return result


def normalized_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 500 or any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise ValueError('Enter a text with 1 to 500 characters')
    return unicodedata.normalize('NFC', ' '.join(text.split()))


class SpeechifyError(RuntimeError):
    def __init__(self, message, *, permanent=False, retry_after=30):
        super().__init__(message)
        self.permanent, self.retry_after = permanent, retry_after


def finalize_streamed_wav(audio):
    """Resolve RIFF streaming sentinels using complete received PCM frames only."""
    unknown = 0xffffffff
    if audio[:4] != b'RIFF' or struct.unpack_from('<I', audio, 4)[0] != unknown:
        return audio
    at, fmt = 12, None
    while at + 8 <= len(audio):
        kind, size = audio[at:at + 4], struct.unpack_from('<I', audio, at + 4)[0]
        start, available = at + 8, len(audio) - at - 8
        if kind == b'data':
            actual = available if size == unknown else size
            if not 0 < actual <= available:
                raise SpeechifyError('Speechify returned incomplete WAV audio data')
            if size == unknown:
                if not fmt:
                    raise SpeechifyError('Speechify streaming audio has no valid format information')
                codec, channels, rate, byte_rate, align, bits = fmt
                allowed_bits = (8, 16, 24, 32) if codec == 1 else (32, 64) if codec == 3 else ()
                if (bits not in allowed_bits or not 1 <= channels <= 8 or not 1000 <= rate <= 192000 or
                        align != channels * (bits // 8) or byte_rate != rate * align or actual % align):
                    raise SpeechifyError('Speechify streaming audio contains incomplete or invalid PCM frames')
                if actual / align / rate > 60:
                    raise SpeechifyError('Speechify audio exceeds 60 seconds')
            result = bytearray(audio)
            struct.pack_into('<I', result, 4, len(result) - 8)
            struct.pack_into('<I', result, at + 4, actual)
            return bytes(result)
        if size > available:
            raise SpeechifyError('Speechify returned an incomplete WAV header')
        if kind == b'fmt ' and size >= 16:
            fields = struct.unpack_from('<HHIIHH', audio, start)
            if fields[0] == 0xfffe and size >= 40:
                fields = (struct.unpack_from('<H', audio, start + 24)[0], *fields[1:])
            fmt = fields
        at = start + size + size % 2
    raise SpeechifyError('Speechify streaming audio has no data section')


def normalized_pcm(audio):
    import miniaudio
    info = miniaudio.wav_get_info(audio)
    if not 0 < info.duration <= 60 or not 1 <= info.nchannels <= 8 or not 1000 <= info.sample_rate <= 192000:
        raise SpeechifyError('Speechify audio has an invalid format or exceeds 60 seconds')
    decoded = miniaudio.decode(audio, nchannels=1, sample_rate=22050, output_format=miniaudio.SampleFormat.SIGNED16)
    pcm = decoded.samples.tobytes()
    if not 0 < len(pcm) <= 22050 * 2 * 60:
        raise SpeechifyError('Speechify audio could not be fully normalized')
    return pcm


class SpeechifyService:
    def __init__(self, root, catalog, *, api_key='', voice='runa-agent', model='simba-3.5', transport=None):
        self.root, self.catalog = Path(root), catalog
        self.api_key, self.voice, self.model = api_key, voice, model
        self.folder = self.root / 'speechify-cache'
        if self.folder.is_symlink():
            raise ValueError('Unexpected link in speech cache')
        self.cache = AudioCache(self.folder, max_entries=2048, max_bytes=256 * 1024 * 1024)
        self.lock, self.transport = asyncio.Lock(), transport or self._http
        self.last_error, self.active_key, self.retry_at, self.next_request = '', None, 0, 0
        self.generated, self.hits = 0, 0

    def key(self, text):
        return hashlib.sha256(('speechify-v1:' + self.model + ':' + self.voice + ':de-DE:wav:' + normalized_text(text)).encode()).hexdigest()

    def update_key(self, value):
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 33 for c in value):
            raise ValueError('Invalid API key')
        self.api_key, self.retry_at, self.last_error = value, 0, ''

    async def _http(self, text):
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
                async with client.post('https://api.speechify.ai/v1/audio/speech',
                                       headers={'Authorization': 'Bearer ' + self.api_key},
                                       json={'input': text, 'voice_id': self.voice, 'model': self.model,
                                             'language': 'de-DE', 'audio_format': 'wav'}, allow_redirects=False) as response:
                    if response.status != 200:
                        messages = {401: 'API key was not accepted', 403: 'Access denied',
                                    429: 'Quota or request limit reached'}
                        retry = response.headers.get('Retry-After', '30')
                        retry = min(300, max(5, int(retry))) if retry.isdigit() else 30
                        raise SpeechifyError('Speechify HTTP ' + str(response.status) + ': ' + messages.get(response.status, 'Speech generation rejected'),
                                             permanent=400 <= response.status < 500 and response.status != 429, retry_after=retry)
                    if response.content_length and response.content_length > 12_000_000:
                        raise SpeechifyError('Speechify response is too large')
                    parts, size = [], 0
                    async for part in response.content.iter_chunked(65536):
                        size += len(part)
                        if size > 12_000_000:
                            raise SpeechifyError('Speechify response is too large')
                        parts.append(part)
                    return json.loads(b''.join(parts))
        except SpeechifyError:
            raise
        except asyncio.TimeoutError:
            raise SpeechifyError('Speechify: speech generation timed out') from None
        except (aiohttp.ClientError, OSError, ValueError):
            raise SpeechifyError('Speechify: connection or response failed') from None

    async def synthesize(self, text):
        text = normalized_text(text)
        key, start = self.key(text), time.perf_counter()
        async with self.lock:
            cached = self.cache.lookup(key)
            if cached:
                self.hits += 1
                result = SpeechResult(cached[0], cached[1], True, round((time.perf_counter() - start) * 1000, 2), provider='speechify', voice='Runa')
                self.catalog.record(result, text, 'speechify', 'Runa', self.model)
                return result
            if not self.api_key:
                raise SpeechifyError('Speechify: API key missing', permanent=True)
            if self.folder.is_symlink():
                raise SpeechifyError('Unexpected link in speech cache', permanent=True)
            self.folder.mkdir(mode=0o700, exist_ok=True)
            if time.time() < self.retry_at:
                raise SpeechifyError(self.last_error or 'Speechify: wait before retrying', retry_after=max(1, self.retry_at-time.time()))
            stats = self.cache.stats()
            if stats['entries'] >= self.cache.max_entries or stats['bytes'] >= self.cache.max_bytes - 8_000_000:
                raise SpeechifyError('Speechify cache is full. Delete entries in the cache viewer.', permanent=True)
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 1.05
            self.active_key = key
            temporary, output = self.folder / (key + '.part.wav'), self.folder / (key + '.wav')
            try:
                data = await self.transport(text)
                if not isinstance(data, dict) or data.get('audio_format') != 'wav' or not isinstance(data.get('audio_data'), str):
                    raise SpeechifyError('Speechify did not return WAV audio')
                audio = base64.b64decode(data['audio_data'], validate=True)
                if not 44 <= len(audio) <= 8_000_000:
                    raise SpeechifyError('Speechify returned an invalid audio size')
                if audio[:4] not in (b'RIFF', b'RF64') or audio[8:12] != b'WAVE':
                    raise SpeechifyError('Speechify did not return a valid WAV file')
                audio = finalize_streamed_wav(audio)
                for path in (temporary, output):
                    if path.is_symlink():
                        raise SpeechifyError('Unexpected link in speech cache', permanent=True)
                temporary.write_bytes(audio)
                temporary.chmod(0o600)
                # Normalize any valid WAV variant to the existing player's 16-bit PCM format.
                try:
                    with wave.open(str(temporary), 'rb') as wav:
                        compatible = wav.getcomptype() == 'NONE' and wav.getsampwidth() == 2 and wav.getnchannels() in (1, 2)
                except (wave.Error, EOFError):
                    compatible = False
                if not compatible:
                    pcm = await asyncio.wait_for(asyncio.to_thread(normalized_pcm, audio), 5)
                    with wave.open(str(temporary), 'wb') as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(22050)
                        wav.writeframes(pcm)
                temporary.replace(output)
                cached = self.cache.lookup(key)
                if not cached:
                    raise SpeechifyError('Speechify audio is incomplete or exceeds 60 seconds')
                result = SpeechResult(cached[0], cached[1], False, round((time.perf_counter()-start)*1000, 2), provider='speechify', voice='Runa')
                self.catalog.record(result, text, 'speechify', 'Runa', self.model)
                self.generated += 1
                self.last_error = ''
                return result
            except BaseException as error:
                output.unlink(missing_ok=True)
                if not isinstance(error, asyncio.CancelledError):
                    safe = str(error) if isinstance(error, SpeechifyError) else 'Speechify: invalid audio data or local storage error'
                    self.last_error = safe
                    self.retry_at = time.time() + getattr(error, 'retry_after', 30)
                    raise SpeechifyError(safe, permanent=getattr(error, 'permanent', False), retry_after=getattr(error, 'retry_after', 30)) from None
                raise
            finally:
                self.active_key = None
                temporary.unlink(missing_ok=True)

    def stats(self):
        return {**self.cache.stats(), 'hasKey': bool(self.api_key), 'voice': 'Runa', 'model': self.model,
                'generated': self.generated, 'hits': self.hits, 'lastError': self.last_error}
