from test_paths import TEST_TMP
"""One real RAOP receiver and one HAP AirPlay-2 receiver, loopback only."""
import asyncio
from array import array
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import wave

from test_pyatv_hap import HapReceiver
from test_pyatv_transport import LoopbackReceiver
from airplay import AirPlayBackend
from audio_gain import attenuate_pcm16

BASE=Path(__file__).resolve().parents[1]


class GroupWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.one=await LoopbackReceiver().start()
        self.two=await HapReceiver().start()
        self.temp=tempfile.TemporaryDirectory(dir=TEST_TMP,prefix='pyatv-group-')
        self.root=Path(self.temp.name)
        self.audio=self.root/'existing-cache.wav'
        self.samples=b'\x12\x34\x56\x78'*100
        with wave.open(str(self.audio),'wb') as wav:
            wav.setnchannels(2);wav.setsampwidth(2);wav.setframerate(44100);wav.writeframes(self.samples)
        self.original=self.audio.read_bytes()
        self.backend=AirPlayBackend(self.root,{'network':'127.0.0.0/8','host':'127.0.0.1'})
        self.backend.native.lead_seconds=self.backend.native.tail_seconds=0
        self.backend.configs={'one':self.one.device,'two':self.two.device}
    async def asyncTearDown(self):
        await self.backend.close()
        await self.one.close();await self.two.close()
        self.assertEqual(self.audio.read_bytes(),self.original)
        self.temp.cleanup()
    async def run_group(self):
        events=[]
        await asyncio.wait_for(self.backend.play_many(['one','two'],self.audio,20,100/44100,
            lambda target,status,error='',timing=None:events.append((target,status,error,timing))),8)
        return events
    async def test_group_reuses_both_authenticated_streams_and_keeps_cache_and_master_gain(self):
        expected=[]
        for gain in (50,100):
            self.backend.set_master_gain(gain)
            events=await self.run_group()
            self.assertEqual(sum(e[1]=='completed' for e in events),2)
            timing=[e[3] for e in events if e[1]=='playing']
            self.assertEqual(len(timing),2)
            self.assertEqual(timing[0]['startUnixMs'],timing[1]['startUnixMs'])
            self.assertTrue(all(t['reusedConnection']==(gain==100) for t in timing))
            samples=array('h');samples.frombytes(attenuate_pcm16(self.samples,gain))
            if sys.byteorder=='little':samples.byteswap()
            expected.append(samples.tobytes())
        self.assertEqual(self.one.connections,1)
        self.assertEqual(len(self.two.wires),1)
        self.assertEqual(self.two.errors,[])
        for audio in (self.one.sink.audio,self.two.audio):
            self.assertEqual(b''.join(audio).replace(b'\0',b''),b''.join(expected).replace(b'\0',b''))
    async def test_one_failed_receiver_does_not_repeat_or_block_the_other(self):
        self.one.fail='RECORD'
        events=await self.run_group()
        self.assertTrue(any(e[:2]==('one','failed') for e in events))
        self.assertTrue(any(e[:2]==('two','completed') for e in events))
        self.assertFalse(any(e[:2]==('one','playing') for e in events))
        self.assertNotIn('one',self.backend.pool.sessions)
        self.assertEqual(self.one.connections,1)
        self.assertEqual(len(self.two.wires),1)

if __name__=='__main__': unittest.main()
