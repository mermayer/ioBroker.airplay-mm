import asyncio
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import struct
import unittest
from unittest.mock import AsyncMock, patch
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from speech import SpeechResult
from speechify_speech import SpeechifyService, SpeechifyError, normalized_text, synthesize_with_fallback
from cache_library import CacheLibrary
from learning import LearningQueue
from core import Engine, UserError
from test_core import FakeBackend


def audio():
    data = io.BytesIO()
    with wave.open(data, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(22050); wav.writeframes(b'\0\0' * 2205)
    return data.getvalue()


class SpeechifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = CacheLibrary(self.root)
        self.transport = AsyncMock(return_value={'audio_format': 'wav', 'audio_data': base64.b64encode(audio()).decode()})
        self.service = SpeechifyService(self.root, self.catalog, api_key='secret-test-key', transport=self.transport)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_single_generation_and_persistent_voice_specific_cache_work_without_a_key(self):
        first, duplicate = await asyncio.gather(self.service.synthesize('Die Tür ist zu.'), self.service.synthesize('Die Tür ist zu.'))
        self.assertFalse(first.cache_hit); self.assertTrue(duplicate.cache_hit); self.assertEqual(self.transport.await_count, 1)
        self.assertEqual(self.catalog.listing()['entries'][0]['voice'], 'Runa')
        other = SpeechifyService(self.root, CacheLibrary(self.root), transport=AsyncMock(side_effect=AssertionError('No network')))
        cached = await other.synthesize('Die Tür ist zu.')
        self.assertTrue(cached.cache_hit); self.assertEqual(cached.path.read_bytes(), audio())
        self.assertNotIn('secret-test-key', json.dumps(self.service.stats()))
        self.assertNotIn('secret-test-key', (self.root / 'cache-index.json').read_text())
        self.assertEqual(normalized_text('  Die\n Tür  ist zu. '), 'Die Tür ist zu.')

    async def test_invalid_audio_does_not_leave_a_broken_entry_and_later_requests_recover(self):
        self.transport.return_value = {'audio_format': 'wav', 'audio_data': 'not-base64!'}
        with self.assertRaises(SpeechifyError): await self.service.synthesize('Kaputt')
        self.assertFalse(list(self.service.folder.iterdir()))
        self.assertFalse(self.catalog.listing()['entries'])
        self.service.retry_at = 0; self.service.next_request = 0
        self.transport.return_value = {'audio_format': 'wav', 'audio_data': base64.b64encode(audio()).decode()}
        self.assertFalse((await self.service.synthesize('Danach')).cache_hit)

    async def test_valid_eight_bit_wave_is_normalized_to_the_existing_airplay_pcm_format(self):
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(1); wav.setframerate(22050); wav.writeframes(b'\x80' * 2205)
        self.transport.return_value = {'audio_format': 'wav', 'audio_data': base64.b64encode(data.getvalue()).decode()}
        result = await self.service.synthesize('Ein anderes WAV-Format')
        with wave.open(str(result.path), 'rb') as wav:
            self.assertEqual(wav.getsampwidth(), 2); self.assertEqual(wav.getframerate(), 22050)
        self.assertEqual(self.catalog.audio(result.path.stem)[1], .1)
        self.assertEqual([p.suffix for p in result.path.parent.iterdir()], ['.wav'])

    async def test_streamed_wave_with_open_lengths_and_list_metadata_keeps_every_pcm_sample(self):
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48000); wav.writeframes(b'\x01\x00' * 4800)
        original = data.getvalue()
        streamed = bytearray(original[:36] + b'LIST' + struct.pack('<I', 26) + b'INFO' + b'\0' * 22 + original[36:])
        struct.pack_into('<I', streamed, 4, 0xffffffff)
        struct.pack_into('<I', streamed, 74, 0xffffffff)
        self.transport.return_value = {'audio_format': 'wav', 'audio_data': base64.b64encode(streamed).decode()}
        result = await self.service.synthesize('Der Tueralarm wurde abgeschaltet!')
        with wave.open(str(result.path), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 4800); self.assertEqual(wav.getframerate(), 48000)
            self.assertEqual(wav.readframes(4801), original[44:])
        self.assertEqual(result.path.read_bytes()[78:], original[44:])
        self.assertEqual(result.duration, .1)
        self.assertEqual(self.catalog.listing()['entries'][0]['voice'], 'Runa')

    async def test_known_length_truncation_is_not_mistaken_for_a_valid_streamed_wave(self):
        damaged = audio()[:-20]
        self.transport.return_value = {'audio_format': 'wav', 'audio_data': base64.b64encode(damaged).decode()}
        with self.assertRaises(SpeechifyError): await self.service.synthesize('Ein abgeschnittener Text')
        self.assertFalse(self.catalog.listing()['entries'])

    async def test_streamed_wave_with_partial_pcm_frame_is_rejected(self):
        damaged = bytearray(audio() + b'\x01')
        struct.pack_into('<I', damaged, 4, 0xffffffff)
        struct.pack_into('<I', damaged, 40, 0xffffffff)
        self.transport.return_value = {'audio_format': 'wav', 'audio_data': base64.b64encode(damaged).decode()}
        with self.assertRaises(SpeechifyError): await self.service.synthesize('Unvollständiger PCM-Rahmen')
        self.assertFalse(self.catalog.listing()['entries'])

    async def test_ramona_takes_over_on_missing_key_or_http_error_and_cached_runa_still_has_priority(self):
        fallback = AsyncMock()
        fallback.synthesize.return_value = SpeechResult(self.root / 'ramona.wav', .1, False, 2)
        self.service.update_key('')
        result = await synthesize_with_fallback(self.service, fallback, 'Test')
        self.assertEqual(result.voice, 'Ramona'); self.assertEqual(result.provider, 'piper'); self.assertIn('key', result.fallback_reason)
        self.assertEqual(self.transport.await_count, 0)
        self.service.update_key('valid-test-key')
        await self.service.synthesize('Test')
        self.service.update_key('')
        result = await synthesize_with_fallback(self.service, fallback, 'Test')
        self.assertEqual(result.voice, 'Runa'); self.assertEqual(fallback.synthesize.await_count, 1)

    async def test_learning_never_plays_audio_and_resume_deduplicates_after_restart(self):
        learning = LearningQueue(self.root, self.service, enabled=True)
        learning.add({'text': 'Ein neuer Text'})
        self.assertEqual(learning.add({'text': 'Ein neuer Text'}), {'alreadyQueued': True})
        resumed = LearningQueue(self.root, self.service, enabled=True)
        resumed.start()
        for _ in range(100):
            if resumed.completed: break
            await asyncio.sleep(.01)
        await resumed.close()
        self.assertEqual(resumed.completed, 1); self.assertFalse(resumed.pending)
        self.assertEqual(self.transport.await_count, 1)
        self.assertEqual(resumed.add({'text': 'Ein neuer Text'}), {'cached': True})
        self.assertFalse((self.root / 'history.json').exists())

    async def test_failed_learning_is_persisted_without_a_fallback_voice_polluting_the_runa_cache(self):
        self.transport.side_effect = SpeechifyError('Speechify HTTP 401: API key was not accepted', permanent=True)
        learning = LearningQueue(self.root, self.service, enabled=True); learning.add({'text': 'Ein Text'}); learning.start()
        for _ in range(100):
            if learning.failed: break
            await asyncio.sleep(.01)
        await learning.close(); self.assertEqual(len(learning.failed), 1)
        self.assertFalse(self.catalog.listing()['entries'])
        resumed = LearningQueue(self.root, self.service, enabled=True); self.assertEqual(len(resumed.failed), 1)
        self.assertEqual(resumed.retry()['retried'], 1)

    async def test_cancelled_online_request_does_not_start_a_fallback(self):
        fallback = AsyncMock(); self.transport.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await synthesize_with_fallback(self.service, fallback, 'Abbruch')
        fallback.synthesize.assert_not_awaited(); self.assertFalse(list(self.service.folder.iterdir()))

    async def test_cache_list_delete_and_invalid_identifiers_preserve_unrelated_files(self):
        result = await self.service.synthesize('Cachetext')
        protected = self.root / 'identity.json'; protected.write_text('protected')
        for key in ['../identity.json', 'x' * 64, '', None]:
            with self.assertRaises(ValueError): self.catalog.delete(key)
        self.assertEqual(protected.read_text(), 'protected')
        self.assertTrue(self.catalog.delete(result.path.stem)['deleted'])
        self.assertFalse(result.path.exists()); self.assertFalse(self.catalog.listing()['entries'])

    async def test_http_request_has_fixed_endpoint_runa_german_and_errors_never_echo_the_key(self):
        recorded = {}
        payload = json.dumps({'audio_format': 'wav', 'audio_data': base64.b64encode(audio()).decode()}).encode()
        class Content:
            async def iter_chunked(self, _): yield payload
        class Response:
            status = 200; headers = {}; content_length = len(payload); content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        response = Response()
        class Client:
            def __init__(self, **options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def post(self, url, **options):
                recorded.update(url=url, **options); return response
        with patch('aiohttp.ClientSession', Client):
            result = await self.service._http('Ein Test')
            self.assertEqual(result['audio_format'], 'wav')
            self.assertEqual(recorded['url'], 'https://api.speechify.ai/v1/audio/speech')
            self.assertEqual(recorded['json']['voice_id'], 'runa-agent'); self.assertEqual(recorded['json']['language'], 'de-DE')
            self.assertEqual(recorded['json']['model'], 'simba-3.5'); self.assertFalse(recorded['allow_redirects'])
            for status in [401, 403, 429, 500]:
                response.status = status
                with self.assertRaises(SpeechifyError) as error: await self.service._http('Ein Test')
                self.assertIn(str(status), str(error.exception)); self.assertNotIn(self.service.api_key, str(error.exception))

    async def test_unknown_legacy_audio_is_visible_and_never_rewritten(self):
        folder = self.root / 'cache'; folder.mkdir(); path = folder / ('a' * 64 + '.wav'); path.write_bytes(audio())
        listing = self.catalog.listing(); self.assertEqual(listing['total'], 1); self.assertEqual(listing['entries'][0]['text'], 'Older cache entry')
        self.assertEqual(self.catalog.audio('a' * 64)[0].read_bytes(), audio())

    async def test_catalog_write_failure_keeps_paid_audio_and_recovers_labels_without_a_second_request(self):
        with patch.object(self.catalog, 'save', side_effect=OSError('disk unavailable')):
            with self.assertLogs('airplay-mm', level='WARNING'):
                result = await self.service.synthesize('Ein dauerhaft vorbereiteter Text')
        self.assertEqual(result.path.read_bytes(), audio())
        self.assertEqual(self.transport.await_count, 1)
        self.service.catalog = CacheLibrary(self.root)
        self.service.update_key('')
        recovered = await self.service.synthesize('Ein dauerhaft vorbereiteter Text')
        self.assertTrue(recovered.cache_hit)
        self.assertEqual(self.transport.await_count, 1)
        self.assertEqual(self.service.catalog.listing()['entries'][0]['text'], 'Ein dauerhaft vorbereiteter Text')

    async def test_cache_play_uses_exact_audio_without_synthesizing_and_cannot_delete_a_queued_entry(self):
        result = await self.service.synthesize('Ein gespeicherter Text')
        catalog = self.catalog
        class Backend(FakeBackend):
            def __init__(self):
                super().__init__(); self.catalog = catalog; self.generations = 0
            def cached_audio(self, key):
                path, duration = self.catalog.audio(key); return SpeechResult(path, duration, True, 0, provider='speechify', voice='Runa')
            async def synthesize(self, text):
                self.generations += 1; raise AssertionError('Cacheplay must not synthesize')
            async def cache_command(self, action, data): return self.catalog.delete(data['key'])
        backend = Backend(); backend.release = asyncio.Event(); engine = Engine(self.root, backend)
        await engine.start(); await engine.discover()
        await engine.play_cached({'key': result.path.stem, 'target': 'lg'})
        await backend.entered.wait()
        with self.assertRaises(UserError): await engine.cache_command('cacheDelete', {'key': result.path.stem})
        backend.release.set(); await engine.queue.join(); self.assertEqual(backend.generations, 0)
        self.assertEqual(engine.snapshot()['history'][0]['voice'], 'Runa')
        self.assertEqual(len(backend.plays), 1); await engine.close()


if __name__ == '__main__': unittest.main()
