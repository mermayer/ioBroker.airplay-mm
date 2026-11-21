"""Wire-level regression on loopback only, with the real unmodified pyatv stack."""
import asyncio
import ipaddress
import plistlib
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from pyatv.const import Protocol
from pyatv_transport import AnnouncementSource, PyatvSession, NTP_EPOCH, RATE


class DatagramSink(asyncio.DatagramProtocol):
    def __init__(self): self.audio = []; self.packets = []
    def datagram_received(self, data, addr):
        self.packets.append(data)
        if len(data)>=12 and data[1]&0x7f==0x60: self.audio.append(data[12:])


class LoopbackReceiver:
    def __init__(self, fail=None):
        self.fail = fail
        self.requests = []
        self.writers = set()
        self.connections = 0
        self.port = 0
        self.sink = DatagramSink()
        self.udp = self.server = None
    async def start(self):
        self.udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda:self.sink, local_addr=('127.0.0.1',0))
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.service = SimpleNamespace(port=self.port, properties={'sr':'44100','ss':'16','ch':'2','et':'0'}, credentials=None, password=None, pairing=None)
        self.device = SimpleNamespace(address=ipaddress.ip_address('127.0.0.1'), name='Loopback', get_service=lambda p:self.service if p==Protocol.RAOP else None)
        return self
    async def serve(self, reader, writer):
        self.connections += 1
        self.writers.add(writer)
        try:
            while True:
                head = await reader.readuntil(b'\r\n\r\n')
                lines = head.decode().split('\r\n')
                verb, route, version = lines[0].split(' ',2)
                headers = {k.lower():v.strip() for k,v in (x.split(':',1) for x in lines[1:] if ':' in x)}
                body = await reader.readexactly(int(headers.get('content-length',0)))
                self.requests.append((verb,route,headers,body))
                payload, extra, status = b'',{},200
                if route=='/info': payload=plistlib.dumps({},fmt=plistlib.FMT_BINARY)
                if verb=='SETUP':
                    port=self.udp.get_extra_info('sockname')[1]
                    extra={'Transport':f'RTP/AVP/UDP;unicast;server_port={port};control_port={port};timing_port={port}','Session':'1'}
                if self.fail==verb: status=500
                response = f'{version} {status} '+('OK' if status==200 else 'Failed')+'\r\n'
                extra={**extra,'CSeq':headers.get('cseq','0'),'Content-Length':str(len(payload))}
                writer.write((response+''.join(k+': '+v+'\r\n' for k,v in extra.items())+'\r\n').encode()+payload)
                await writer.drain()
        except (asyncio.IncompleteReadError,ConnectionError): pass
        finally:
            self.writers.discard(writer)
            writer.close()
            await writer.wait_closed()
    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for w in list(self.writers): w.close()
        await asyncio.gather(*(w.wait_closed() for w in list(self.writers)),return_exceptions=True)
        self.udp.close()


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = await LoopbackReceiver().start()
        self.session=PyatvSession(Path('not-written'), '127.0.0.1', self.device.device, 'loopback', '1',timeout=2)
    async def asyncTearDown(self):
        await self.session.close()
        await self.device.close()
    async def test_two_clips_use_one_rtsp_session_and_preserve_every_sample(self):
        await self.session.start()
        first=b'\x12\x34\x56\x78'*100
        for pcm in (first,first):
            info=await self.session.prepare(pcm,20)
            self.assertEqual(info['reused'],self.session.used)
            anchor=await self.session.begin(info['minimumStartMs']+100)
            self.assertGreater(anchor,time.time()*1000)
            await self.session.finish(len(pcm)/(44100*4),10)
        self.assertEqual(self.device.connections,1)
        self.assertEqual(sum(v=='RECORD' for v,*_ in self.device.requests),1)
        self.assertEqual(sum(v=='SETUP' for v,*_ in self.device.requests),1)
        self.assertTrue(self.session.live())
        audio=b''.join(self.device.sink.audio).replace(b'\0',b'')
        self.assertEqual(audio,b'\x34\x12\x78\x56'*200)
    async def test_record_failure_is_not_a_success_and_cleans_up(self):
        self.device.fail='RECORD'
        with self.assertRaises(RuntimeError): await self.session.start()
        await self.session.close()
        self.assertFalse(self.session.live())
        self.assertTrue(self.session.stream_task.done())
    async def test_cancellation_after_start_closes_without_replay(self):
        await self.session.start()
        info=await self.session.prepare(b'\x11\x22\x33\x44'*100,20)
        await self.session.begin(info['minimumStartMs']+100)
        task=asyncio.create_task(self.session.finish(10,10))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await self.session.close()
        self.assertEqual(self.device.connections,1)
        self.assertFalse(self.session.live())
        self.assertTrue(self.session.stream_task.done())
        self.assertFalse(b''.join(self.device.sink.audio).replace(b'\0',b''))
    async def test_socket_loss_fails_and_does_not_open_a_replacement(self):
        await self.session.start()
        for w in list(self.device.writers): w.close()
        await asyncio.sleep(.05)
        with self.assertRaises(RuntimeError): await self.session.prepare(b'\x11'*400,10)
        self.assertEqual(self.device.connections,1)


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduling_inside_packets_and_idle_never_replays_audio(self):
        c=SimpleNamespace(head_ts=NTP_EPOCH*RATE,latency=0)
        source=AnnouncementSource(c)
        source.prepare(b'\x12\x34\x56\x78'*441)
        source.schedule(10)
        output=[]
        for n in range(6):
            output.append(await source.readframes(352)); c.head_ts+=352
        self.assertTrue(source.consumed.is_set())
        self.assertEqual(b''.join(output).replace(b'\0',b''),b'\x34\x12\x78\x56'*441)
        self.assertEqual(source.sent_bytes,441*4)
        source.release()
        self.assertEqual(await source.readframes(352),bytes(352*4))
    async def test_prepared_audio_stays_silent_until_begin(self):
        c=SimpleNamespace(head_ts=NTP_EPOCH*RATE,latency=0)
        s=AnnouncementSource(c);s.prepare(b'\x11'*400)
        self.assertEqual(await s.readframes(352),bytes(352*4))
        self.assertFalse(s.consumed.is_set())
        await s.close()
        self.assertEqual(await s.readframes(352),b'')


if __name__=='__main__': unittest.main()
