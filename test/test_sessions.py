from test_paths import TEST_TMP
import asyncio
from array import array
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from airplay import AirPlayBackend
from pairing import Credentials
from sessions import NativeSession, SessionPool

def receiver(address='10.0.0.200', name='LG'):
    return SimpleNamespace(name=name, address=address,
        get_service=lambda protocol: SimpleNamespace(port=7000, properties={}))

class VirtualSession:
    instances = []
    fail = None
    release = None

    def __init__(self, root, host, device, target, mode, credential):
        self.receiver, self.target = device, target
        self.mode, self.credential = mode, credential
        self.closed = self.used = False
        self.phase = 'connecting'
        self.last_used = time.monotonic()
        self.starts, self.anchors, self.pcms = 0, [], []
        self.instances.append(self)

    def signature(self):
        return str(self.receiver.address), self.mode, self.credential

    async def start(self):
        self.starts += 1
        await asyncio.sleep(0)

    def live(self):
        return not self.closed

    async def prepare(self, pcm, volume):
        self.phase = 'prepared'
        self.pcms.append(pcm)
        if self.target == self.fail:
            raise ConnectionError('Receiver nicht verfügbar')
        return {'reused': self.used, 'minimumStartMs': 0}

    async def begin(self, at):
        self.phase = 'playing'
        self.anchors.append(at)
        return at

    async def finish(self, duration, keep):
        if self.release:
            await self.release.wait()
        self.phase = 'idle'
        self.used = True
        if keep is None:
            await self.close()

    async def close(self):
        self.closed = True
        self.phase = 'closed'


class SessionPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = TEST_TMP
        folder.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder, prefix='pool-')
        self.root = Path(self.temp.name)
        VirtualSession.instances = []
        VirtualSession.fail = VirtualSession.release = None
        self.backend = AirPlayBackend(self.root, {'network': '10.0.0.0/24', 'host': '10.0.0.210'})
        self.backend.pool.factory = VirtualSession
        self.backend.native.tail_seconds = self.backend.native.lead_seconds = 0
        self.backend.configs = {'lg': receiver(), 'node': receiver('10.0.0.203', 'NODE')}
        self.patch = patch('airplay.miniaudio.decode_file', return_value=SimpleNamespace(samples=array('h', [121] * 200)))
        self.decode = self.patch.start()

    async def asyncTearDown(self):
        await self.backend.close()
        self.patch.stop()
        self.temp.cleanup()

    async def play(self):
        events = []
        await self.backend.play_many(['lg', 'node'], Path('virtual.wav'), 15, .01,
            lambda target, status, error='', timing=None: events.append((target, status, error, timing)))
        return events

    async def test_two_announcements_reuse_each_process_and_one_decode_per_group(self):
        await self.play()
        events = await self.play()
        sessions = self.backend.pool.sessions
        self.assertEqual(sum(s.starts for s in sessions.values()), 2)
        self.assertEqual(self.decode.call_count, 2)
        self.assertIs(sessions['lg'].pcms[0], sessions['node'].pcms[0])
        self.assertEqual(sessions['lg'].anchors, sessions['node'].anchors)
        self.assertTrue(all(e[3]['reusedConnection'] for e in events if e[1] == 'playing'))

    async def test_failed_receiver_keeps_other_receiver_running(self):
        VirtualSession.fail = 'node'
        events = await self.play()
        self.assertTrue(any(e[:2] == ('lg', 'completed') for e in events))
        self.assertTrue(any(e[:2] == ('node', 'failed') for e in events))
        self.assertNotIn('node', self.backend.pool.sessions)

    async def test_no_keepalive_closes_connections_after_each_job(self):
        self.backend.pool.enabled = False
        await self.play()
        self.assertFalse(self.backend.pool.snapshot())
        self.assertTrue(all(s.closed for s in self.backend.pool.sessions.values()))

    async def test_manual_disconnect_then_next_announcement_reconnects(self):
        await self.play()
        old = self.backend.pool.sessions['lg']
        await self.backend.pool.discard('lg')
        self.assertTrue(old.closed)
        await self.play()
        self.assertIsNot(self.backend.pool.sessions['lg'], old)

    async def test_cancellation_closes_all_job_connections_without_replay(self):
        VirtualSession.release = asyncio.Event()
        task = asyncio.create_task(self.play())
        async def playing():
            while len([s for s in self.backend.pool.sessions.values() if s.phase == 'playing']) != 2:
                await asyncio.sleep(.001)
        await asyncio.wait_for(playing(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.backend.pool.snapshot())
        self.assertEqual(sum(len(s.anchors) for s in VirtualSession.instances), 2)

    async def test_eight_connection_limit_evicts_idle_sessions(self):
        pool = self.backend.pool
        for n in range(8):
            session = await pool.get(receiver(), str(n), 'native-ntp')
            session.phase = 'idle'
        first = pool.sessions['0']
        await pool.get(receiver(), 'ninth', 'native-ntp')
        self.assertEqual(len(pool.sessions), 8)
        self.assertTrue(first.closed)

    async def test_key_change_discards_the_old_authenticated_connection(self):
        old = await self.backend.pool.get(receiver(), 'lg', 'native-ntp')
        old.phase = 'idle'
        self.backend.credentials.save('lg', {'credentials': 'ab' * 96, 'dacp': '0123456789ABCDEF'})
        new = await self.backend.pool.get(receiver(), 'lg', 'native-ntp')
        self.assertTrue(old.closed)
        self.assertEqual(new.credential['credentials'], 'ab' * 96)
