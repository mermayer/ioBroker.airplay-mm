from test_paths import TEST_TMP
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from core import Engine, UserError, validate_request


class FakeBackend:
    def __init__(self):
        self.plays = []
        self.entered = asyncio.Event()
        self.release = None
        self.fail_once = False
        self.master_gain = 100

    def set_master_gain(self, level):
        self.master_gain = level

    async def discover(self):
        return [{'id': 'lg', 'name': 'Example Soundbar', 'address': '10.0.0.200', 'available': True}]

    async def synthesize(self, text):
        await asyncio.sleep(0)
        return Path(text), 1.0

    async def play(self, target, audio, volume, duration, on_playing):
        self.plays.append((target, str(audio), volume))
        on_playing()
        self.entered.set()
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError('Receiver hat die Verbindung verloren')
        if self.release:
            await self.release.wait()
        await asyncio.sleep(0)


class Validation(unittest.TestCase):
    def request(self, **changes):
        return {'text': 'Hallo Welt', 'target': 'lg', 'volume': 15, **changes}

    def test_valid_unicode(self):
        result = validate_request(self.request(text=' Küche, Straße und Grüße! '))
        self.assertEqual(result['text'], 'Küche, Straße und Grüße!')
        self.assertTrue(result['requestId'])

    def test_invalid_text(self):
        for text in ['', '  ', None, 42, 'x' * 501, 'Hallo\x00Welt']:
            with self.subTest(text=repr(text)[:30]), self.assertRaises(UserError):
                validate_request(self.request(text=text))

    def test_text_limit(self):
        self.assertEqual(len(validate_request(self.request(text='ü' * 500))['text']), 500)

    def test_volume_limits(self):
        for volume in [True, False, None, '15', -1, 41, float('inf'), float('nan')]:
            with self.subTest(volume=volume), self.assertRaises(UserError):
                validate_request(self.request(volume=volume))
        for volume in [0, 15, 40, 12.5]:
            self.assertEqual(validate_request(self.request(volume=volume))['volume'], volume)

    def test_invalid_target(self):
        for target in ['', None, 3, 'x' * 129]:
            with self.subTest(target=target), self.assertRaises(UserError):
                validate_request(self.request(target=target))

    def test_unknown_fields_and_request_identifier(self):
        for changes in [{'url': 'http://example.invalid'}, {'requestId': 'a b'}, {'requestId': 'x' * 97}]:
            with self.subTest(changes=changes), self.assertRaises(UserError):
                validate_request(self.request(**changes))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP, prefix='engine-')
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to((TEST_TMP).resolve())
        self.backend = FakeBackend()
        self.engine = Engine(self.root, self.backend)
        await self.engine.start()
        await self.engine.discover()

    async def asyncTearDown(self):
        await self.engine.close()
        assert self.root.is_relative_to((TEST_TMP).resolve())
        self.temp.cleanup()

    async def submit(self, text='Hallo Welt', **extra):
        return await self.engine.submit({'target': 'lg', 'text': text, 'volume': 15, **extra})

    async def idle(self):
        await asyncio.wait_for(self.engine.queue.join(), 2)

    async def test_real_queue_lifecycle(self):
        row = await self.submit()
        await self.idle()
        self.assertEqual(self.engine.history[-1]['status'], 'completed')
        self.assertEqual(len(self.backend.plays), 1)
        self.assertEqual(self.backend.plays[0], ('lg', 'Hallo Welt', 15))
        self.assertEqual(self.engine.history[-1]['id'], row['id'])
        self.assertFalse(self.engine.snapshot()['busy'])

    async def test_idempotent_submission(self):
        a = await self.submit(requestId='same-request')
        b = await self.submit(requestId='same-request')
        await self.idle()
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(len(self.backend.plays), 1)

    async def test_identifier_cannot_change_content(self):
        await self.submit(requestId='same-request')
        with self.assertRaises(UserError):
            await self.submit('Ein anderer Text', requestId='same-request')
        await self.idle()
        self.assertEqual(len(self.backend.plays), 1)

    async def test_device_failure_does_not_block_next_job(self):
        self.backend.fail_once = True
        await self.submit('Erste Ansage')
        await self.submit('Zweite Ansage')
        await self.idle()
        self.assertEqual([row['status'] for row in self.engine.history], ['failed', 'completed'])
        self.assertIn('ConnectionError', self.engine.history[0]['error'])
        self.assertFalse(self.engine.runner.done())

    async def test_timeout_does_not_block_queue(self):
        self.engine.job_timeout = .02
        self.backend.release = asyncio.Event()
        await self.submit('Queued endlos')
        await self.idle()
        self.assertEqual(self.engine.history[-1]['status'], 'failed')
        self.backend.release = None
        self.engine.job_timeout = 1
        await self.submit('Danach erfolgreich')
        await self.idle()
        self.assertEqual(self.engine.history[-1]['status'], 'completed')

    async def test_cancel_active_and_queued(self):
        self.backend.release = asyncio.Event()
        await self.submit('Aktiv')
        await self.backend.entered.wait()
        await self.submit('Queued')
        self.assertEqual(await self.engine.cancel(), 2)
        await self.idle()
        self.assertEqual([row['status'] for row in self.engine.history], ['cancelled', 'cancelled'])
        self.assertEqual(len(self.backend.plays), 1)

    async def test_bounded_queue(self):
        self.backend.release = asyncio.Event()
        await self.submit('Aktiv')
        await self.backend.entered.wait()
        for index in range(8):
            await self.submit(f'Queued {index}')
        with self.assertRaises(UserError):
            await self.submit('Zu viel')
        self.assertEqual(self.engine.queue.qsize(), 8)
        await self.engine.cancel()
        await self.idle()

    async def test_offline_receiver_is_rejected(self):
        self.engine.devices[0]['available'] = False
        with self.assertRaises(UserError):
            await self.submit()
        self.assertFalse(self.backend.plays)

    async def test_unknown_receiver_is_rejected(self):
        with self.assertRaises(UserError):
            await self.submit(target='http://10.0.0.200/')
        self.assertFalse(self.backend.plays)

    async def test_history_and_settings_survive_restart_without_replay(self):
        await self.submit('Einmal', requestId='persisted')
        await self.idle()
        await self.engine.close()
        self.engine = Engine(self.root, self.backend)
        await self.engine.start()
        await self.engine.discover()
        await self.submit('Einmal', requestId='persisted')
        await self.idle()
        self.assertEqual(len(self.backend.plays), 1)
        self.assertEqual(self.engine.settings['target'], 'lg')

    async def test_interrupted_job_is_not_replayed(self):
        await self.engine.close()
        (self.root / 'history.json').write_text(json.dumps([{'id': 'old', 'status': 'playing', 'text': 'Alt'}]))
        self.engine = Engine(self.root, self.backend)
        await self.engine.start()
        self.assertEqual(self.engine.history[-1]['status'], 'interrupted')
        await asyncio.sleep(.01)
        self.assertFalse(self.backend.plays)

    async def test_broken_history_is_preserved_for_diagnosis(self):
        await self.engine.close()
        (self.root / 'history.json').write_text('{ungültig', encoding='utf-8')
        self.engine = Engine(self.root, self.backend)
        await self.engine.start()
        self.assertEqual(len(list(self.root.glob('history.json.unlesbar-*'))), 1)
        self.assertFalse(self.engine.runner.done())

    async def test_disk_error_rejects_new_job_without_playing(self):
        with patch('core.atomic_json', side_effect=OSError('Datenträger voll')):
            with self.assertRaises(UserError):
                await self.submit()
        self.assertEqual(self.engine.queue.qsize(), 0)
        self.assertFalse(self.backend.plays)

    async def test_disk_error_during_completion_does_not_crash_runner(self):
        self.backend.release = asyncio.Event()
        await self.submit()
        await self.backend.entered.wait()
        with patch('core.atomic_json', side_effect=OSError('Datenträger voll')):
            self.backend.release.set()
            await self.idle()
        self.backend.release = None
        await self.submit('Next')
        await self.idle()
        self.assertEqual(self.engine.history[-1]['status'], 'completed')
        self.assertFalse(self.engine.runner.done())

    async def test_shutdown_interrupts_active_job(self):
        self.backend.release = asyncio.Event()
        await self.submit()
        await self.backend.entered.wait()
        await asyncio.wait_for(self.engine.close(), 1)
        self.assertEqual(self.engine.history[-1]['status'], 'interrupted')
        self.assertTrue(self.engine.runner.done())

    async def test_load_200_announcements_is_serial_and_bounded(self):
        for index in range(200):
            await self.submit(f'Ansage {index}', requestId=f'soak-{index}')
            await self.idle()
        self.assertEqual(len(self.backend.plays), 200)
        self.assertEqual(len(self.engine.history), 100)
        self.assertLess((self.root / 'history.json').stat().st_size, 100_000)

    async def test_speech_timings_and_cache_result_are_visible_in_history(self):
        from speech import SpeechResult
        async def synthesize(text):
            return SpeechResult(Path('cached.wav'), 1.25, True, .2)
        self.backend.synthesize = synthesize
        await self.submit()
        await self.idle()
        row = self.engine.snapshot()['history'][0]
        self.assertTrue(row['cacheHit'])
        self.assertGreaterEqual(row['generationMs'], 0)
        self.assertGreaterEqual(row['connectMs'], 0)
        self.assertEqual(row['durationSeconds'], 1.25)

    async def test_receiver_timing_is_preserved_as_scheduled_not_measured_start(self):
        async def play(target,audio,volume,duration,on_playing):
            on_playing({'receiverBufferMs':2000,'preRollMs':350})
        self.backend.play = play
        await self.submit()
        await self.idle()
        row = self.engine.snapshot()['history'][0]
        self.assertEqual(row['receiverBufferMs'],2000)
        self.assertEqual(row['preRollMs'],350)
        self.assertGreaterEqual(row['estimatedStartMs'],2350)
        self.assertEqual(row['status'],'completed')

    async def test_invalid_timing_metadata_cannot_poison_status_json(self):
        async def play(target,audio,volume,duration,on_playing):
            on_playing({'receiverBufferMs':float('nan'),'preRollMs':-1})
        self.backend.play = play
        await self.submit()
        await self.idle()
        row = self.engine.snapshot()['history'][0]
        self.assertNotIn('receiverBufferMs',row)
        self.assertNotIn('preRollMs',row)
        self.assertNotIn('estimatedStartMs',row)

    async def test_failed_prepare_is_reported_and_a_later_job_still_works(self):
        original = self.backend.synthesize
        async def broken(text):
            raise RuntimeError('Sprachmodell fehlt')
        self.backend.synthesize = broken
        with self.assertRaises(RuntimeError):
            await self.engine.precache({'text': 'Hallo'})
        self.assertFalse(self.engine.precaching)
        self.assertIn('Sprachmodell fehlt', self.engine.snapshot()['lastError'])
        self.backend.synthesize = original
        await self.submit()
        await self.idle()
        self.assertEqual(self.engine.history[-1]['status'], 'completed')

    async def test_only_one_prepare_runs_and_its_cancellation_releases_the_guard(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(text):
            entered.set()
            await release.wait()
            return Path('audio.wav'), 1
        self.backend.synthesize = waiting
        task = asyncio.create_task(self.engine.precache({'text': 'Hallo'}))
        await entered.wait()
        with self.assertRaises(UserError):
            await self.engine.precache({'text': 'Doppelt'})
        self.assertTrue(self.engine.snapshot()['preparingText'])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.engine.precaching)
        self.assertFalse(self.backend.plays)


if __name__ == '__main__':
    unittest.main()
