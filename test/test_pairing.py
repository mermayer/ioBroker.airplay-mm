from test_paths import TEST_TMP
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'worker'))
from core import UserError
from pairing import Credentials,Pairing,redact

KEY='ab'*32+':'+'cd'*32+':706f64:636c69656e74'
OLD={'credentials':'ef'*96,'dacp':'0123456789ABCDEF'}

class FakeHandler:
    def __init__(self): self.service=SimpleNamespace(credentials=KEY);self.has_paired=False;self.closed=False;self.value=None;self.block=None
    async def begin(self): pass
    def pin(self,value): self.value=value
    async def finish(self):
        if self.block: await self.block.wait()
        if self.value!='1234': raise ValueError('wrong pin '+str(self.value))
        self.has_paired=True
    async def close(self): self.closed=True

class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=TEST_TMP,prefix='pairing-')
        self.root=Path(self.temp.name);self.store=Credentials(self.root)
        self.handlers=[]
        async def factory(*args,**kwargs):
            h=FakeHandler();self.handlers.append(h);return h
        self.pairing=Pairing(self.root,self.store,timeout=1,verify_timeout=.2,factory=factory)
        self.receiver=SimpleNamespace(name='Fake TV',get_service=lambda p:True)
    async def asyncTearDown(self): await self.pairing.cancel();self.temp.cleanup()
    async def start(self): return await self.pairing.start(self.receiver,'tv')
    async def finish(self,pin='1234'):
        await self.pairing.finish('tv',pin)
        await asyncio.wait_for(self.pairing.task,1)
    async def test_success_keeps_credentials_out_of_public_state(self):
        self.assertEqual((await self.start())['phase'],'pin')
        await self.finish()
        self.assertEqual(self.pairing.snapshot()['phase'],'paired')
        self.assertEqual(Credentials(self.root).get('tv')['pyatv'],KEY)
        self.assertNotIn(KEY,json.dumps(self.pairing.snapshot()))
        self.assertTrue(self.handlers[-1].closed)
    async def test_wrong_pin_preserves_existing_credentials(self):
        self.store.save_pyatv('tv',KEY);before=self.store.pyatv_path.read_bytes()
        await self.start();await self.finish('9999')
        self.assertEqual(self.pairing.snapshot()['phase'],'failed')
        self.assertEqual(self.store.pyatv_path.read_bytes(),before)
        self.assertNotIn('9999',json.dumps(self.pairing.snapshot()))
    async def test_cancel_closes_handler_and_cannot_create_keys(self):
        await self.start();await self.pairing.cancel(clear=True)
        self.assertTrue(self.handlers[-1].closed)
        self.assertIsNone(self.pairing.snapshot())
        self.assertFalse(self.store.pyatv_path.exists())
    async def test_second_pairing_and_mismatched_target_rejected(self):
        await self.start()
        with self.assertRaises(UserError): await self.pairing.start(self.receiver,'other')
        with self.assertRaises(UserError): await self.pairing.finish('other','1234')
    async def test_invalid_pin_does_not_change_phase(self):
        await self.start()
        for pin in ('','1','12345','abcd',1234,None):
            with self.assertRaises(UserError): await self.pairing.finish('tv',pin)
        self.assertEqual(self.pairing.snapshot()['phase'],'pin')
    async def test_timeout_then_new_pairing(self):
        self.pairing.timeout=.03
        await self.start();await asyncio.sleep(.06)
        self.assertEqual(self.pairing.snapshot()['phase'],'failed')
        self.assertTrue(self.handlers[-1].closed)
        self.pairing.timeout=1
        self.assertEqual((await self.start())['phase'],'pin')
    async def test_verification_timeout_does_not_store_keys(self):
        await self.start();self.handlers[-1].block=asyncio.Event()
        await self.finish()
        self.assertEqual(self.pairing.snapshot()['phase'],'failed')
        self.assertFalse(self.store.pyatv_path.exists())
        self.assertTrue(self.handlers[-1].closed)
    async def test_unreadable_old_file_is_not_overwritten(self):
        self.store.path.write_text('{broken',encoding='utf-8')
        invalid=Credentials(self.root)
        self.assertTrue(invalid.error)
        with self.assertRaises(UserError): invalid.save_pyatv('tv',KEY)
        self.assertEqual(self.store.path.read_text(),'{broken')
    async def test_new_keys_leave_old_file_byte_identical(self):
        self.store.save('tv',OLD);before=self.store.path.read_bytes()
        self.store.save_pyatv('tv',KEY)
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(Credentials(self.root).get('tv')['pyatv'],KEY)
    async def test_forget_one_preserves_other_and_removes_both_key_formats(self):
        for target in ('tv','pod'):
            self.store.save(target,OLD);self.store.save_pyatv(target,KEY)
        self.store.forget('tv')
        fresh=Credentials(self.root)
        self.assertIsNone(fresh.get('tv'));self.assertIsNotNone(fresh.get('pod'))
    async def test_redacts_both_private_key_formats(self):
        for value in (KEY,OLD['credentials']): self.assertNotIn(value,redact('CREDENTIALS: '+value))

if __name__=='__main__': unittest.main()
