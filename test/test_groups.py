from test_paths import TEST_TMP
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from core import Engine, UserError, validate_request
from test_core import FakeBackend

class MultiBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.generations = 0
        self.fail_target = None

    async def discover(self):
        return [dict(id='lg', name='LG', available=True), dict(id='node', name='NODE', available=True)]

    async def synthesize(self, text):
        self.generations += 1
        return await super().synthesize(text)

    async def play(self, target, audio, volume, duration, on_playing):
        if target == self.fail_target:
            raise ConnectionError('Dieser Receiver ist ausgefallen')
        return await super().play(target, audio, volume, duration, on_playing)

class GroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = TEST_TMP
        folder.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder, prefix='groups-')
        self.root = Path(self.temp.name)
        self.backend = MultiBackend()
        self.engine = Engine(self.root, self.backend)
        await self.engine.start()
        await self.engine.discover()

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    async def test_one_generation_serves_both_targets_and_keeps_single_target_api(self):
        await self.engine.submit({'text': 'Hallo', 'targets': ['lg', 'node'], 'requestId': 'multi'})
        await asyncio.wait_for(self.engine.queue.join(), 1)
        row = self.engine.snapshot()['history'][0]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual([x['status'] for x in row['targetResults']], ['completed', 'completed'])
        self.assertEqual(self.backend.generations, 1)
        self.assertEqual({x[0] for x in self.backend.plays}, {'lg', 'node'})
        await self.engine.submit({'text': 'Alt', 'target': 'lg', 'requestId': 'old'})
        await self.engine.queue.join()
        self.assertNotIn('targets', self.engine.snapshot()['history'][0])

    async def test_failed_target_does_not_prevent_success_at_other_target(self):
        self.backend.fail_target = 'node'
        await self.engine.submit({'text': 'Hallo', 'targets': ['lg', 'node']})
        await self.engine.queue.join()
        row = self.engine.snapshot()['history'][0]
        self.assertEqual(row['status'], 'partial')
        self.assertEqual(row['targetResults'][0]['status'], 'completed')
        self.assertEqual(row['targetResults'][1]['status'], 'failed')
        self.backend.fail_target = None
        await self.engine.submit({'text': 'Danach', 'targets': ['lg', 'node']})
        await self.engine.queue.join()
        self.assertEqual(self.engine.snapshot()['history'][0]['status'], 'completed')

    async def test_repeated_request_id_does_not_repeat_either_receiver(self):
        data = {'text': 'Einmal', 'targets': ['lg', 'node'], 'requestId': 'same'}
        first = await self.engine.submit(data)
        await self.engine.queue.join()
        repeat = await self.engine.submit(data)
        self.assertEqual(first['id'], repeat['id'])
        self.assertEqual(len(self.backend.plays), 2)

    async def test_invalid_selections_do_not_generate_audio_or_mutate_history(self):
        for targets in [[], ['lg', 'lg'], 'lg', [1], ['x' * 129], ['lg'] * 9]:
            with self.assertRaises(UserError):
                await self.engine.submit({'text': 'Hallo', 'targets': targets})
        with self.assertRaises(UserError):
            await self.engine.submit({'text': 'Hallo', 'targets': ['lg'], 'target': 'node'})
        self.assertEqual(self.backend.generations, 0)
        self.assertFalse(self.engine.history)

    async def test_saved_selection_and_history_survive_restart_without_playback(self):
        self.engine.update_settings({'targets': ['lg', 'node'], 'volume': 19})
        await self.engine.submit({'text': 'Hallo', 'targets': ['lg', 'node'], 'volume': 19})
        await self.engine.queue.join()
        await self.engine.close()
        backend = MultiBackend()
        self.engine = Engine(self.root, backend)
        await self.engine.start()
        self.assertEqual(self.engine.settings['targets'], ['lg', 'node'])
        self.assertEqual(self.engine.snapshot()['history'][0]['status'], 'completed')
        self.assertFalse(backend.plays)
        self.assertFalse(self.engine.active)

    async def test_setting_legacy_target_clears_multi_selection(self):
        self.engine.update_settings({'targets': ['lg', 'node']})
        self.engine.update_settings({'target': 'lg'})
        self.assertNotIn('targets', self.engine.settings)

    async def test_cancelling_multi_request_ends_both_active_calls(self):
        self.backend.release = asyncio.Event()
        await self.engine.submit({'text': 'Hallo', 'targets': ['lg', 'node']})
        await self.backend.entered.wait()
        await self.engine.cancel()
        await self.engine.queue.join()
        self.assertEqual(self.engine.snapshot()['history'][0]['status'], 'cancelled')
        self.assertTrue(all(x['status'] == 'cancelled' for x in self.engine.snapshot()['history'][0]['targetResults']))

    async def test_snapshot_does_not_mutate_saved_selection_or_nested_history(self):
        self.engine.update_settings({'targets': ['lg', 'node']})
        snap = self.engine.snapshot()
        snap['settings']['targets'].append('tv')
        snap['devices'][0]['name'] = 'changed'
        self.assertEqual(self.engine.settings['targets'], ['lg', 'node'])
        self.assertNotEqual(self.engine.devices[0]['name'], 'changed')

    async def test_cancelled_queued_group_has_no_stranded_pending_receivers(self):
        self.backend.release = asyncio.Event()
        await self.engine.submit({'text': 'Erste', 'targets': ['lg', 'node']})
        await self.backend.entered.wait()
        await self.engine.submit({'text': 'Zweite', 'targets': ['lg', 'node']})
        await self.engine.cancel()
        await self.engine.queue.join()
        for row in self.engine.snapshot()['history']:
            self.assertEqual(row['status'], 'cancelled')
            self.assertTrue(all(x['status'] == 'cancelled' for x in row['targetResults']))

    async def test_pairing_cannot_take_over_a_receiver_with_a_queued_announcement(self):
        self.backend.release = asyncio.Event()
        self.backend.pairing_command = lambda action, data: self.fail('Pairing must not start')
        await self.engine.submit({'text': 'Blockiert', 'target': 'lg'})
        await self.backend.entered.wait()
        await self.engine.submit({'text': 'Queued', 'target': 'node'})
        with self.assertRaisesRegex(UserError, 'active or queued'):
            await self.engine.pairing_command('pairStart', {'target': 'node'})
        await self.engine.cancel()
        await self.engine.queue.join()

    async def test_pairing_can_be_cancelled_after_receiver_disappears_from_discovery(self):
        self.backend.pairing_status = lambda: {'target': 'vanished', 'phase': 'pin'}
        async def cancel(action, data):
            self.assertEqual(action, 'pairCancel')
            return {'cancelled': True}
        self.backend.pairing_command = cancel
        result = await self.engine.pairing_command('pairCancel', {'target': 'vanished'})
        self.assertEqual(result, {'cancelled': True})

    async def test_public_history_stays_below_bridge_limit_with_large_multi_errors(self):
        for n in range(100):
            self.engine.history.append({'id': str(n), 'status': 'partial', 'error': 'ü' * 1000,
                'targetResults': [{'name': 'ü' * 80, 'error': 'ü' * 600, 'status': 'failed'} for _ in range(8)]})
        self.engine._persist()
        self.assertLess(len(json.dumps(self.engine.snapshot(), ensure_ascii=True)), 300000)
        await self.engine.close()
        self.engine = Engine(self.root, MultiBackend())
        await self.engine.start()
        self.assertEqual(len(self.engine.history), 100)

if __name__ == '__main__':
    unittest.main()
