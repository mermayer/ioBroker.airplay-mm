from test_paths import TEST_TMP
from array import array
import asyncio
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from audio_gain import attenuate_pcm16, playback_wave, async_playback_wave, validate_gain
from airplay import AirPlayBackend
from core import Engine, UserError
from test_core import FakeBackend
from test_groups import MultiBackend


class GainSamples(unittest.TestCase):
    def test_full_level_is_byte_identical_and_half_level_reduces_only_amplitude(self):
        raw = array('h', [-32768, -30000, -2, 0, 2, 30000, 32767]).tobytes()
        self.assertIs(attenuate_pcm16(raw, 100), raw)
        result = array('h'); result.frombytes(attenuate_pcm16(raw, 50))
        self.assertEqual(list(result), [-16384, -15000, -1, 0, 1, 15000, 16383])
        self.assertEqual(len(attenuate_pcm16(raw, 25)), len(raw))

    def test_zero_level_is_silence_with_the_same_frame_count(self):
        raw = array('h', [-32768, 32767, 100]).tobytes()
        self.assertEqual(attenuate_pcm16(raw, 0), bytes(len(raw)))

    def test_invalid_gain_and_incomplete_samples_are_rejected(self):
        for value in [-1, 101, True, False, None, '50', float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError): validate_gain(value)
        with self.assertRaises(ValueError): attenuate_pcm16(b'x', 50)


class GainFiles(unittest.TestCase):
    def setUp(self):
        folder=TEST_TMP;folder.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder,prefix='gain-')
        self.root=Path(self.temp.name).resolve();assert self.root.is_relative_to(folder.resolve())
        self.audio=self.root/'cached.wav'
        self.pcm=array('h',[20000,-20000,1000,-1000]*100).tobytes()
        with wave.open(str(self.audio),'wb') as audio:
            audio.setnchannels(2);audio.setsampwidth(2);audio.setframerate(22050);audio.writeframes(self.pcm)
        self.original=self.audio.read_bytes()

    def tearDown(self): self.temp.cleanup()

    def test_temporary_wave_preserves_format_duration_and_cached_file(self):
        with playback_wave(self.audio,self.root,50) as temporary:
            self.assertNotEqual(temporary,self.audio);self.assertTrue(temporary.is_file())
            with wave.open(str(temporary),'rb') as audio:
                self.assertEqual((audio.getnchannels(),audio.getsampwidth(),audio.getframerate()),(2,2,22050))
                self.assertEqual(audio.getnframes(),200)
                self.assertEqual(audio.readframes(200),attenuate_pcm16(self.pcm,50))
            self.assertEqual(self.audio.read_bytes(),self.original)
        self.assertFalse(temporary.exists())
        self.assertEqual(self.audio.read_bytes(),self.original)

    def test_full_level_needs_no_temporary_copy_and_failure_cleans_up(self):
        with playback_wave(self.audio,self.root,100) as source: self.assertEqual(source,self.audio)
        self.assertFalse((self.root/'playback-audio').exists())
        with self.assertRaises(ConnectionError):
            with playback_wave(self.audio,self.root,25) as temporary: raise ConnectionError('Offline')
        self.assertFalse(temporary.exists());self.assertEqual(self.audio.read_bytes(),self.original)


class SignalSession:
    def __init__(self): self.used=False;self.calls=[]
    async def prepare(self,pcm,volume):
        self.calls.append((pcm,volume));return {'reused':self.used,'minimumStartMs':0}
    async def begin(self,at): return at
    async def finish(self,duration,keep): self.used=True


class SignalPool:
    enabled=True;keep_seconds=300
    def __init__(self): self.sessions={}
    async def get(self,receiver,target,mode): return self.sessions.setdefault(target,SignalSession())
    async def discard(self,target): self.sessions.pop(target,None)


class BackendGain(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder=TEST_TMP;folder.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder,prefix='gain-backend-');self.root=Path(self.temp.name)
        self.audio=self.root/'cached.wav';self.pcm=array('h',[24000,-24000,8000,-8000]*50).tobytes()
        with wave.open(str(self.audio),'wb') as audio:
            audio.setnchannels(2);audio.setsampwidth(2);audio.setframerate(44100);audio.writeframes(self.pcm)
        self.digest=hashlib.sha256(self.audio.read_bytes()).hexdigest()
        self.backend=AirPlayBackend.__new__(AirPlayBackend);self.backend.root=self.root
        self.backend.config={'defaultMode':'native-ntp'}
        self.backend.configs={key:SimpleNamespace(address=address) for key,address in [('one','10.0.0.200'),('two','10.0.0.225')]}
        self.backend.native=SimpleNamespace(lead_seconds=0,tail_seconds=0)
        self.backend.pool=SignalPool()

    async def asyncTearDown(self):
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).hexdigest(),self.digest)
        self.temp.cleanup()

    async def test_native_group_scales_once_and_preserves_both_receiver_volumes_and_warm_sessions(self):
        self.backend.set_master_gain(50);events=[]
        main_thread=threading.get_ident();conversion_threads=[]
        def scale(pcm,level): conversion_threads.append(threading.get_ident());return attenuate_pcm16(pcm,level)
        with patch('airplay.miniaudio.decode_file',return_value=SimpleNamespace(samples=array('h',[24000,-24000,8000,-8000]*50))) as decode, patch('airplay.attenuate_pcm16',side_effect=scale):
            await self.backend.play_many(['one','two'],self.audio,35,.1,lambda *args,**kwargs:events.append(args))
            decode.assert_called_once()
        self.assertEqual(len(conversion_threads),1);self.assertNotEqual(conversion_threads[0],main_thread)
        for session in self.backend.pool.sessions.values(): self.assertEqual(session.calls,[(attenuate_pcm16(self.pcm,50),35)])
        first=self.backend.pool.sessions['one'];self.backend.set_master_gain(25)
        with patch('airplay.miniaudio.decode_file',return_value=SimpleNamespace(samples=array('h',[24000,-24000,8000,-8000]*50))):
            await self.backend.play('one',self.audio,35,.1,lambda *args:None)
        self.assertIs(self.backend.pool.sessions['one'],first)
        self.assertEqual(first.calls[-1],(attenuate_pcm16(self.pcm,25),35))
        self.assertEqual(sum(event[1]=='completed' for event in events),2)

    async def test_legacy_stream_uses_scaled_copy_and_the_original_receiver_volume(self):
        self.backend.config['defaultMode']='2';seen=[]
        async def stream(receiver,audio,volume,duration,on_playing,mode):
            with wave.open(str(audio),'rb') as wav: pcm=wav.readframes(wav.getnframes())
            seen.append((audio,pcm,volume,mode));on_playing()
        self.backend._legacy_stream=stream
        for level in [50,25,100]:
            self.backend.set_master_gain(level)
            await self.backend.play('one',self.audio,35,.1,lambda:None)
            path,pcm,volume,mode=seen[-1]
            self.assertEqual(pcm,attenuate_pcm16(self.pcm,level));self.assertEqual(volume,35);self.assertEqual(mode,'2')
            self.assertEqual(path==self.audio,level==100)
            if level!=100:self.assertFalse(path.exists())

    async def test_cancelling_legacy_playback_removes_only_the_temporary_copy(self):
        self.backend.config['defaultMode']='1';self.backend.set_master_gain(50);entered=asyncio.Event();paths=[]
        async def stream(receiver,audio,*args): paths.append(audio);entered.set();await asyncio.Event().wait()
        self.backend._legacy_stream=stream
        task=asyncio.create_task(self.backend.play('one',self.audio,35,.1,lambda:None))
        await asyncio.wait_for(entered.wait(),1);self.assertTrue(paths[0].exists());task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(paths[0].exists());self.assertTrue(self.audio.exists())

    async def test_cancelling_during_file_preparation_cleans_the_late_result(self):
        entered=threading.Event();release=threading.Event()
        @contextmanager
        def delayed(source,root,level):
            entered.set();release.wait(2)
            with playback_wave(source,root,level) as path:yield path
        async def job():
            async with async_playback_wave(self.audio,self.root,50): self.fail('Cancelled work must not stream')
        with patch('audio_gain.playback_wave',delayed):
            task=asyncio.create_task(job())
            self.assertTrue(await asyncio.to_thread(entered.wait,1));task.cancel()
            await asyncio.sleep(0);release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(list((self.root/'playback-audio').glob('*.wav')),[])


class GainEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder=TEST_TMP;folder.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder,prefix='gain-engine-');self.root=Path(self.temp.name)
        self.backend=MultiBackend();self.engine=Engine(self.root,self.backend,max_volume=80)
        await self.engine.start();await self.engine.discover()

    async def asyncTearDown(self): await self.engine.close();self.temp.cleanup()

    async def test_default_preserves_old_settings_and_master_change_does_not_play(self):
        self.engine.update_settings({'volume':35,'target':'lg'})
        self.assertEqual(self.engine.settings,{'volume':35,'target':'lg'});self.assertEqual(self.engine.snapshot()['masterGain'],100)
        self.engine.update_settings({'masterGain':50})
        self.assertEqual(self.engine.settings['volume'],35);self.assertEqual(self.backend.plays,[])
        await self.engine.close();self.engine=Engine(self.root,MultiBackend(),max_volume=80);await self.engine.start()
        self.assertEqual(self.engine.snapshot()['masterGain'],50);self.assertEqual(self.engine.settings['volume'],35)

    async def test_invalid_level_and_storage_failure_preserve_saved_settings(self):
        self.engine.update_settings({'masterGain':50});before=self.engine.settings.copy()
        for value in [-1,101,True,'50',None,float('nan')]:
            with self.subTest(value=value),self.assertRaises(UserError): self.engine.update_settings({'masterGain':value})
        with patch('core.atomic_json',side_effect=OSError('Full')):
            with self.assertRaises(UserError):self.engine.update_settings({'masterGain':20})
        self.assertEqual(self.engine.settings,before);self.assertEqual(self.backend.plays,[])

    async def test_all_targets_use_one_global_gain_even_with_an_explicit_receiver_level(self):
        self.engine.update_settings({'masterGain':50})
        await self.engine.submit({'text':'Beide Empfaenger','targets':['lg','node'],'volume':60})
        await asyncio.wait_for(self.engine.queue.join(),1)
        self.assertEqual(self.backend.master_gain,50);self.assertEqual([play[2] for play in self.backend.plays],[60,60])
        self.assertEqual(self.engine.history[-1]['masterGain'],50);self.assertEqual(self.engine.history[-1]['status'],'completed')

    async def test_master_cannot_be_overridden_by_a_speak_request(self):
        self.engine.update_settings({'masterGain':20})
        with self.assertRaises(UserError):await self.engine.submit({'text':'Test','target':'lg','masterGain':100})
        self.assertEqual(self.engine.snapshot()['masterGain'],20);self.assertEqual(self.backend.plays,[])

    async def test_cached_playback_applies_master_without_synthesizing_again(self):
        key='a'*64
        self.backend.cached_audio=lambda selected:(Path('original-cached.wav'),1.0)
        self.backend.catalog=SimpleNamespace(index={key:{'text':'Vorhandene Ansage'}})
        self.engine.update_settings({'masterGain':25})
        await self.engine.play_cached({'key':key,'target':'lg','volume':35})
        await asyncio.wait_for(self.engine.queue.join(),1)
        self.assertEqual(self.backend.generations,0);self.assertEqual(self.backend.master_gain,25)
        self.assertEqual(self.engine.history[-1]['masterGain'],25);self.assertEqual(self.backend.plays[-1][2],35)

    async def test_an_active_announcement_keeps_its_level_and_the_next_uses_the_new_setting(self):
        self.backend.release=asyncio.Event();self.engine.update_settings({'masterGain':50})
        await self.engine.submit({'text':'Erste Ansage','target':'lg','volume':35})
        await asyncio.wait_for(self.backend.entered.wait(),1)
        self.engine.update_settings({'masterGain':20});self.assertEqual(self.backend.master_gain,50)
        self.backend.release.set();await asyncio.wait_for(self.engine.queue.join(),1)
        self.backend.release=None
        await self.engine.submit({'text':'Naechste Ansage','target':'lg','volume':35})
        await asyncio.wait_for(self.engine.queue.join(),1)
        self.assertEqual(self.backend.master_gain,20);self.assertEqual([row['masterGain'] for row in self.engine.history],[50,20])
