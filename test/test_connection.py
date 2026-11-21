"""Connection failures and receiver clock probes, with loopback traffic only."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worker'))
from pyatv.protocols.raop.protocols import TimingServer
from pyatv.protocols.raop.packets import TimingPacket
from pyatv_transport import PyatvSession


class Reply(asyncio.DatagramProtocol):
    def __init__(self): self.reply = asyncio.get_running_loop().create_future()
    def datagram_received(self, data, address):
        if not self.reply.done(): self.reply.set_result(data)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        receiver = SimpleNamespace(address='127.0.0.1')
        return PyatvSession(Path('not-written'), '127.0.0.1', receiver, 'loopback', '2')

    async def test_clock_probe_is_counted_and_answered_without_changing_timestamp_echo(self):
        session = self.session()
        loop = asyncio.get_running_loop()
        transport, timing = await loop.create_datagram_endpoint(TimingServer, local_addr=('127.0.0.1', 0))
        sender, peer = await loop.create_datagram_endpoint(Reply, local_addr=('127.0.0.1', 0))
        session.client = SimpleNamespace(timing_server=timing, close=lambda: timing.close())
        original = timing.datagram_received
        try:
            session.track_timing()
            sender.sendto(TimingPacket.encode(0x80, 0xd2, 7, 0, 0, 0, 0, 0, 123, 456), ('127.0.0.1', timing.port))
            packet = TimingPacket.decode(await asyncio.wait_for(peer.reply, 1))
            self.assertEqual(session.timing_requests, 1)
            self.assertEqual((packet.reftime_sec, packet.reftime_frac), (123, 456))
            self.assertEqual(packet.type & 0x7f, 0x53)
            await session.close()
            self.assertEqual(timing.datagram_received, original)
            self.assertTrue(transport.is_closing())
        finally:
            sender.close()
            await session.close()

    async def test_missing_clock_probe_produces_actionable_error_and_closes_partial_connection(self):
        session = self.session()
        async def fail():
            session.connect_stage = 'audio_setup'
            session.timing_port = 45678
            raise TimeoutError('private authentication data')
        with patch.object(session, '_start', fail):
            with self.assertRaisesRegex(RuntimeError, 'inbound UDP timing at 127.0.0.1:45678') as raised:
                await session.start()
        self.assertNotIn('private authentication data', str(raised.exception))
        self.assertTrue(session.closed)
        self.assertFalse(session.live())

    async def test_clock_traffic_prevents_incorrect_firewall_diagnosis(self):
        session = self.session()
        session.connect_stage, session.timing_port, session.timing_requests = 'audio_setup', 45678, 2
        message = session.connection_error(TimeoutError('private data'))
        self.assertNotIn('Firewall', message)
        self.assertNotIn('private data', message)

    async def test_connection_cancellation_closes_resources_and_is_not_retried(self):
        session = self.session()
        async def cancel(): raise asyncio.CancelledError()
        with patch.object(session, '_start', cancel):
            with self.assertRaises(asyncio.CancelledError): await session.start()
        self.assertTrue(session.closed)
        self.assertEqual(session.phase, 'closed')


if __name__ == '__main__': unittest.main()
