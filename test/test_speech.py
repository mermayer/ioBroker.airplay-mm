from test_paths import TEST_TMP
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from speech import AudioCache, SpeechService, cache_key

def wav(path, frames=2205):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b'\0\0' * frames)

class CacheTests(unittest.TestCase):
    def setUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_key_preserves_previous_version(self):
        import hashlib
        self.assertEqual(cache_key('Hallo Welt'), hashlib.sha256(b'piper-1.8.0:thorsten-medium:Hallo Welt').hexdigest())
        self.assertNotEqual(cache_key('Hallo Welt'), cache_key('Hallo Welt.'))

    def test_different_local_voices_never_share_a_cache_key(self):
        self.assertNotEqual(cache_key('Hallo Welt'), cache_key('Hallo Welt', 'de_DE-ramona-low'))
        with self.assertRaises(ValueError):
            cache_key('Hallo', '../../fremde-stimme')

    def test_existing_audio_is_reused(self):
        key = cache_key('Fertig')
        wav(self.folder / (key + '.wav'))
        result = AudioCache(self.folder).lookup(key)
        self.assertAlmostEqual(result[1], .1)

    def test_invalid_or_truncated_audio_is_removed_for_regeneration(self):
        key = cache_key('Fertig')
        for broken in [b'invalid', None]:
            file = self.folder / (key + '.wav')
            wav(file)
            file.write_bytes(broken or file.read_bytes()[:44])
            self.assertIsNone(AudioCache(self.folder).lookup(key))
            self.assertFalse(file.exists())

    def test_lru_bounds_count_and_leaves_recent_audio(self):
        for number, text in enumerate(['alt', 'neu', 'jetzt']):
            file = self.folder / (cache_key(text) + '.wav')
            wav(file)
            os.utime(file, (number + 1, number + 1))
        cache = AudioCache(self.folder, max_entries=2)
        cache.prune()
        self.assertEqual(cache.stats()['entries'], 2)
        self.assertFalse((self.folder / (cache_key('alt') + '.wav')).exists())

    def test_byte_budget_keeps_current_file(self):
        files = []
        for text in ['a', 'b', 'c']:
            file = self.folder / (cache_key(text) + '.wav')
            wav(file)
            files.append(file)
        cache = AudioCache(self.folder, max_bytes=5000)
        cache.prune(keep=files[0])
        self.assertTrue(files[0].exists())
        self.assertLessEqual(cache.stats()['bytes'], 5000)

    def test_status_handles_unreadable_cache(self):
        with patch.object(AudioCache, 'files', side_effect=OSError('nicht lesbar')):
            self.assertEqual(AudioCache(self.folder).stats()['entries'], 0)

class PersistentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name)
        (self.root / 'cache').mkdir()
        self.children = []
        self.scenarios = []
        self.launch = AsyncMock(side_effect=self.create)
        self.service = SpeechService(self.root, timeout=.05, launch=self.launch)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def create(self, *args, **kwargs):
        voice = args[args.index('--voice') + 1]
        scenario = self.scenarios.pop(0) if self.scenarios else 'normal'
        ended = asyncio.Event()
        child = SimpleNamespace(returncode=None, terminated=0)
        child.stdout, child.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        child.stderr.feed_eof()
        def stop():
            child.terminated += 1
            child.returncode = -15
            ended.set()
        child.terminate = stop
        child.kill = stop
        async def wait():
            await ended.wait()
            return child.returncode
        child.wait = wait
        def write(data):
            request = json.loads(data)
            child.text = request['text']
            if scenario == 'hang':
                return
            if scenario == 'crash':
                child.returncode = 1
                ended.set()
                child.stdout.feed_eof()
                return
            key = cache_key(request['text'], voice)
            wav(self.root / 'cache' / (key + '.wav'))
            child.stdout.feed_data((json.dumps({'id': request['id'] if scenario != 'wrong-id' else 'wrong', 'ok': True, 'key': key}) + '\n').encode())
        child.stdin = SimpleNamespace(write=write, drain=AsyncMock())
        if scenario == 'no-model':
            child.stdout.feed_data(b'{"event":"fatal","error":"Modell fehlt"}\n')
        else:
            child.stdout.feed_data((json.dumps({'event':'ready','voice':voice,'modelLoadMs':10})+'\n').encode())
        self.children.append(child)
        return child

    async def test_model_is_loaded_once_and_exact_text_is_cached(self):
        await self.service.start()
        first = await self.service.synthesize('Küche fertig')
        await self.service.synthesize('Ein anderer Text')
        cached = await self.service.synthesize('Küche fertig')
        self.assertFalse(first.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(self.launch.await_count, 1)
        self.assertEqual(self.service.stats()['hits'], 1)
        self.assertEqual(self.service.stats()['entries'], 2)

    async def test_cache_hit_needs_no_running_tts_process(self):
        wav(self.root / 'cache' / (cache_key('Hallo') + '.wav'))
        result = await self.service.synthesize('Hallo')
        self.assertTrue(result.cache_hit)
        self.launch.assert_not_awaited()

    async def test_crash_is_isolated_and_next_text_starts_a_new_worker(self):
        self.scenarios = ['crash', 'normal']
        with self.assertRaises(RuntimeError):
            await self.service.synthesize('Erster Text')
        result = await self.service.synthesize('Zweiter Text')
        self.assertFalse(result.cache_hit)
        self.assertEqual(self.launch.await_count, 2)

    async def test_timeout_terminates_only_the_tts_worker(self):
        self.scenarios = ['hang']
        with self.assertRaises(TimeoutError):
            await self.service.synthesize('Warten')
        self.assertEqual(self.children[0].terminated, 1)
        self.assertIsNone(self.service.process)

    async def test_cancellation_stops_inference_and_a_later_request_can_run(self):
        self.scenarios = ['hang', 'normal']
        task = asyncio.create_task(self.service.synthesize('Warten'))
        while not self.children or not hasattr(self.children[0], 'text'):
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse((await self.service.synthesize('Danach')).cache_hit)

    async def test_missing_model_does_not_poison_existing_cache(self):
        self.scenarios = ['no-model']
        with self.assertRaisesRegex(RuntimeError, 'Modell fehlt'):
            await self.service.start()
        wav(self.root / 'cache' / (cache_key('Vorhanden') + '.wav'))
        self.assertTrue((await self.service.synthesize('Vorhanden')).cache_hit)

    async def test_mismatched_protocol_is_rejected_and_unconfirmed_audio_removed(self):
        self.scenarios = ['wrong-id']
        with self.assertRaises(RuntimeError):
            await self.service.synthesize('Falsch')
        self.assertFalse((self.root / 'cache' / (cache_key('Falsch') + '.wav')).exists())

    async def test_invalid_text_and_closed_service_cannot_generate_audio(self):
        for text in ['', 'x' * 501, 'bad\x00text']:
            with self.assertRaises(ValueError):
                await self.service.synthesize(text)
        self.launch.assert_not_awaited()
        await self.service.close()
        with self.assertRaises(RuntimeError):
            await self.service.synthesize('Später')

    async def test_switch_to_ramona_keeps_thorsten_audio_and_generates_a_new_file(self):
        old = await self.service.synthesize('Gleicher Text')
        await self.service.close()
        self.service = SpeechService(self.root, voice='de_DE-ramona-low', timeout=.05, launch=self.launch)
        new = await self.service.synthesize('Gleicher Text')
        self.assertFalse(new.cache_hit)
        self.assertNotEqual(old.path, new.path)
        self.assertTrue(old.path.is_file())
        self.assertEqual(self.service.stats()['voice'], 'Ramona · German · Local')
        self.assertTrue((await self.service.synthesize('Gleicher Text')).cache_hit)

    async def test_ramona_cache_survives_restart_without_starting_the_model(self):
        key = cache_key('Readys vorhanden', 'de_DE-ramona-low')
        wav(self.root / 'cache' / (key + '.wav'))
        self.service = SpeechService(self.root, voice='de_DE-ramona-low', launch=self.launch)
        result = await self.service.synthesize('Readys vorhanden')
        self.assertTrue(result.cache_hit)
        self.launch.assert_not_awaited()
