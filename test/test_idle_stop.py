"""Persistent connection lifetime tests."""
import asyncio
from pathlib import Path
import unittest
from test_pyatv_transport import LoopbackReceiver,PyatvSession

class IdleStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device=await LoopbackReceiver().start()
        self.session=PyatvSession(Path('not-written'),'127.0.0.1',self.device.device,'loop','1',timeout=2)
        await self.session.start()
    async def asyncTearDown(self): await self.session.close();await self.device.close()
    async def test_repeated_simultaneous_close_has_one_cleanup(self):
        await asyncio.gather(*(self.session.close() for _ in range(5)))
        self.assertTrue(self.session.stream_task.done())
        self.assertTrue(self.session.audio_transport.is_closing())
        self.assertFalse(self.session.live())
        self.assertEqual(self.device.connections,1)
    async def test_prepare_cancels_expiry_without_reconnecting(self):
        self.session.idle_task=asyncio.create_task(self.session._expire(.05))
        await self.session.prepare(b'x'*400,10)
        await asyncio.sleep(.07)
        self.assertTrue(self.session.live())
        self.assertIsNone(self.session.idle_task)
        self.assertEqual(self.device.connections,1)
    async def test_idle_expiry_releases_every_owned_socket(self):
        await self.session._expire(.01)
        self.assertFalse(self.session.live())
        self.assertTrue(self.session.stream_task.done())
        self.assertTrue(self.session.audio_transport.is_closing())
        self.assertIsNone(self.session.connection.transport)

if __name__=='__main__': unittest.main()
