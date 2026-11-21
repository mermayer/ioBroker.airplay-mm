from test_paths import TEST_TMP
import asyncio
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from sayit_speech import SayItSpeechService, cache_key, decode_chunks
from speech import cache_key as piper_key

def audio_chunk(value=1000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(value.to_bytes(2, 'little', signed=True) * 12000)
    return base64.b64encode(buffer.getvalue()).decode()

class DecodeTests(unittest.TestCase):
    def test_all_parts_are_decoded_and_joined_without_losing_the_end(self):
        pcm = decode_chunks([audio_chunk(1000), audio_chunk(-1000)])
        self.assertEqual(len(pcm), 24000 * 2 + 3840)
        self.assertEqual(int.from_bytes(pcm[-2:], 'little', signed=True), -1000)

    def test_invalid_or_oversized_audio_is_rejected(self):
        for chunks in [None, [], ['bad base64!'], ['x' * 1_400_001], [audio_chunk()] * 33]:
            with self.subTest(chunks=str(chunks)[:20]), self.assertRaises((ValueError, RuntimeError)):
                decode_chunks(chunks)

class SayItTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name)
        (self.root / 'cache').mkdir()
        self.scenarios = []
        self.children = []
        self.launch = AsyncMock(side_effect=self.create)
        self.service = SayItSpeechService(self.root, timeout=.04, launch=self.launch)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def create(self, *args, **kwargs):
        scenario = self.scenarios.pop(0) if self.scenarios else 'ok'
        ended = asyncio.Event()
        process = SimpleNamespace(returncode=None, terminated=0)
        process.stdout, process.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        process.stderr.feed_eof()
        def respond(text=None):
            if scenario == 'hang':
                return
            data = {'ok': True, 'provider': 'sayit-google', 'language': 'de', 'sayitVersion': '5.3.2'}
            if text is not None:
                data.update(key=cache_key(text) if scenario != 'wrong-key' else 'wrong', chunks=[audio_chunk(), audio_chunk(-1000)])
            if scenario == 'offline':
                data = {'ok': False, 'error': 'Google ist unreachable'}
            process.stdout.feed_data(json.dumps(data).encode())
            process.stdout.feed_eof()
            process.returncode = 1 if scenario == 'offline' else 0
            ended.set()
        if args[-1] == '--check':
            respond()
        def write(data):
            process.text = json.loads(data)['text']
            respond(process.text)
        process.stdin = SimpleNamespace(write=write, drain=AsyncMock(), close=lambda: None)
        async def wait():
            await ended.wait()
            return process.returncode
        def terminate():
            process.terminated += 1
            process.returncode = -15
            process.stdout.feed_eof()
            ended.set()
        process.wait = wait
        process.terminate = terminate
        process.kill = terminate
        self.children.append(process)
        return process

    async def test_realistic_generation_caches_complete_audio_and_reuses_it(self):
        await self.service.start()
        generated = await self.service.synthesize('Hallo Welt')
        cached = await self.service.synthesize('Hallo Welt')
        self.assertFalse(generated.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertAlmostEqual(generated.duration, 1.08)
        self.assertEqual(self.launch.await_count, 2)  # probe, one generation
        self.assertEqual(self.service.stats()['provider'], 'sayit-google')

    async def test_piper_file_is_retained_and_not_mistaken_for_google(self):
        old = self.root / 'cache' / (piper_key('Hallo') + '.wav')
        old.write_bytes(base64.b64decode(audio_chunk()))
        new = await self.service.synthesize('Hallo')
        self.assertFalse(new.cache_hit)
        self.assertNotEqual(old, new.path)
        self.assertTrue(old.is_file())

    async def test_cache_survives_restart_and_works_without_sayit_or_network(self):
        first = await self.service.synthesize('Ohne Internet')
        await self.service.close()
        self.service = SayItSpeechService(self.root, launch=AsyncMock(side_effect=RuntimeError('offline')))
        cached = await self.service.synthesize('Ohne Internet')
        self.assertTrue(cached.cache_hit)
        self.assertEqual(first.path, cached.path)
        self.service.launch.assert_not_awaited()

    async def test_online_failure_never_falls_back_to_a_different_voice(self):
        self.scenarios = ['offline', 'ok']
        with self.assertRaisesRegex(RuntimeError, 'Google'):
            await self.service.synthesize('Erster Text')
        self.assertFalse((self.root / 'cache' / (cache_key('Erster Text') + '.wav')).exists())
        self.assertFalse((await self.service.synthesize('Danach')).cache_hit)
        self.assertEqual(len(self.children), 2)

    async def test_timeout_and_cancellation_stop_the_helper_and_leave_the_service_usable(self):
        self.scenarios = ['hang', 'hang', 'ok']
        with self.assertRaises(TimeoutError):
            await self.service.synthesize('Warten')
        task = asyncio.create_task(self.service.synthesize('Cancel'))
        while len(self.children) < 2 or not hasattr(self.children[1], 'text'):
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([x.terminated for x in self.children], [1, 1])
        self.assertFalse((await self.service.synthesize('Next')).cache_hit)

    async def test_wrong_text_response_and_invalid_input_cannot_create_a_cached_file(self):
        self.scenarios = ['wrong-key']
        with self.assertRaises(RuntimeError):
            await self.service.synthesize('Falsche Antwort')
        self.assertEqual(list((self.root / 'cache').iterdir()), [])
        for value in ['', None, 'x' * 501, 'Hallo\x00']:
            with self.assertRaises(ValueError):
                await self.service.synthesize(value)
        self.assertEqual(self.launch.await_count, 1)

    async def test_probe_failure_is_reported_and_closed_service_cannot_generate(self):
        self.scenarios = ['offline']
        with self.assertRaises(RuntimeError):
            await self.service.start()
        self.assertIn('Google', self.service.last_error)
        await self.service.close()
        with self.assertRaises(RuntimeError):
            await self.service.synthesize('Danach')
