from test_paths import TEST_TMP
"""Real HAP/SRP, encrypted RTSP and encrypted RTP against loopback only.

The test server uses pyatv's MIT server-auth implementation and binds only to loopback.
"""
import asyncio
import ipaddress
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import time
import unittest

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from pyatv.auth.hap_session import HAPSession
from pyatv.conf import AppleTV, ManualService
from pyatv.const import Protocol
from pyatv.protocols.airplay.server_auth import BaseAirPlayServerAuth
from pyatv.support.http import BasicHttpServer, HttpResponse

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'worker'))
from pyatv_transport import PyatvSession
from pyatv_pairing import PyatvPairing
from pairing import Credentials


class HapRouter(BaseAirPlayServerAuth):
    def __init__(self,receiver):
        super().__init__('HomePod Test',unique_id='loopback-homepod',pin=1111)
        self.receiver=receiver
        self.wire=None
        for method in ('SETUP','RECORD','FLUSH','TEARDOWN','SET_PARAMETER','OPTIONS'):
            self.add_route(method,'.*',self.answer)
        for method,path in [('GET','/info'),('POST','/feedback')]: self.add_route(method,'^'+path+'$',self.answer)
    def enable_encryption(self,output_key,input_key):
        self.wire.pending=HAPSession()
        self.wire.pending.enable(output_key,input_key)
    def answer(self,request):
        self.receiver.requests.append((request.method,request.path))
        result={}
        if request.method=='SETUP':
            value=plistlib.loads(request.body)
            if 'streams' in value:
                self.receiver.audio_key=value['streams'][0]['shk']
                port=self.receiver.udp.get_extra_info('sockname')[1]
                result={'streams':[{'dataPort':port,'controlPort':port}]}
            else: result={'eventPort':self.receiver.event_port}
        body=plistlib.dumps(result,fmt=plistlib.FMT_BINARY) if request.method=='SETUP' or request.path=='/info' else b''
        return HttpResponse(request.protocol,request.version,200,'OK',{'CSeq':request.headers.get('CSeq','0'),'Content-Type':'application/x-apple-binary-plist'},body)


class HapWire(BasicHttpServer):
    def __init__(self,receiver):
        router=HapRouter(receiver)
        super().__init__(router)
        router.wire=self
        self.active=self.pending=None
        receiver.wires.append(self)
    def process_received(self,data): return self.active.decrypt(data) if self.active else data
    def process_sent(self,data):
        if self.pending:
            self.active,self.pending=self.pending,None
            return data
        return self.active.encrypt(data) if self.active else data


class EncryptedSink(asyncio.DatagramProtocol):
    def __init__(self,receiver): self.receiver=receiver
    def datagram_received(self,data,addr):
        if len(data)>36 and data[1]&0x7f==0x60:
            try:
                pcm=ChaCha20Poly1305(self.receiver.audio_key).decrypt(bytes(4)+data[-8:],data[12:-8],data[4:12])
                self.receiver.audio.append(pcm)
            except Exception as e: self.receiver.errors.append(type(e).__name__)


class HapReceiver:
    def __init__(self): self.requests=[];self.audio=[];self.errors=[];self.wires=[];self.events=[];self.audio_key=None
    async def start(self):
        loop=asyncio.get_running_loop()
        self.udp,_=await loop.create_datagram_endpoint(lambda:EncryptedSink(self),local_addr=('127.0.0.1',0))
        async def event(reader,writer): self.events.append(writer);await reader.read()
        self.event=await asyncio.start_server(event,'127.0.0.1',0)
        self.event_port=self.event.sockets[0].getsockname()[1]
        self.server=await loop.create_server(lambda:HapWire(self),'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]
        self.device=AppleTV(ipaddress.ip_address('127.0.0.1'),'HomePod Test')
        self.device.add_service(ManualService('loopback-homepod',Protocol.AirPlay,self.port,{'features':'0x00000000,0x10040'}))
        return self
    async def close(self):
        self.server.close();self.event.close()
        for wire in self.wires:
            if wire.transport: wire.transport.close()
        for writer in self.events: writer.close()
        await self.server.wait_closed();await self.event.wait_closed()
        await asyncio.gather(*(w.wait_closed() for w in self.events),return_exceptions=True)
        self.udp.close()
        await asyncio.sleep(.01)


class HapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.receiver=await HapReceiver().start()
        self.temp=tempfile.TemporaryDirectory(dir=TEST_TMP,prefix='pyatv-hap-')
        self.root=Path(self.temp.name)
        self.store=Credentials(self.root)
        self.pairing=PyatvPairing(self.root,self.store,verify_timeout=3)
        self.session=None
    async def asyncTearDown(self):
        await self.pairing.cancel()
        if self.session: await self.session.close()
        await self.receiver.close()
        self.temp.cleanup()
    async def pair(self,pin='1111'):
        self.assertEqual((await self.pairing.start(self.receiver.device,'pod'))['phase'],'pin')
        await self.pairing.finish('pod',pin)
        await asyncio.wait_for(self.pairing.task,4)
        return self.pairing.snapshot()
    async def test_real_pin_pairing_persists_private_keys_and_preserves_original_file(self):
        old={'credentials':'ab'*96,'dacp':'0123456789ABCDEF'}
        self.store.save('pod',old)
        original=self.store.path.read_bytes()
        result=await self.pair()
        self.assertEqual(result['phase'],'paired')
        self.assertEqual(self.store.path.read_bytes(),original)
        new=Credentials(self.root).get('pod')
        self.assertTrue(self.store.valid_pyatv(new['pyatv']))
        self.assertNotIn(new['pyatv'],json.dumps(result))
    async def test_wrong_pin_preserves_existing_keys_and_closes_connection(self):
        self.assertEqual((await self.pair())['phase'],'paired')
        original=self.store.pyatv_path.read_bytes()
        result=await self.pair('9999')
        self.assertEqual(result['phase'],'failed')
        self.assertEqual(self.store.pyatv_path.read_bytes(),original)
        self.assertIsNone(self.pairing.handler)
    async def test_transient_airplay2_encryption_and_reused_connection(self):
        self.session=PyatvSession(self.root,'127.0.0.1',self.receiver.device,'pod','2',timeout=3)
        await self.session.start()
        for _ in range(2):
            pcm=b'\x12\x34\x56\x78'*100
            info=await self.session.prepare(pcm,20)
            await self.session.begin(info['minimumStartMs']+100)
            await self.session.finish(len(pcm)/176400,10)
        self.assertEqual(len(self.receiver.wires),1)
        self.assertEqual(self.receiver.requests.count(('RECORD',self.session.client.rtsp.uri)),1)
        self.assertEqual(self.receiver.errors,[])
        self.assertEqual(b''.join(self.receiver.audio).replace(b'\0',b''),b'\x34\x12\x78\x56'*200)
    async def test_saved_hap_keys_reconnect_with_pair_verify(self):
        self.assertEqual((await self.pair())['phase'],'paired')
        self.session=PyatvSession(self.root,'127.0.0.1',self.receiver.device,'pod','2',self.store.get('pod'),timeout=3)
        await self.session.start()
        self.assertTrue(self.session.live())
        self.assertTrue(any(w.active for w in self.receiver.wires))


if __name__=='__main__': unittest.main()
