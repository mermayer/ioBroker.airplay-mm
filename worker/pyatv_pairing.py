"""Own bounded PIN workflow using pyatv's MIT-licensed pairing API."""
import asyncio
import copy
import re
import time

import pyatv
from pyatv.const import Protocol
from pyatv.storage.memory_storage import MemoryStorage

from core import UserError


class PyatvPairing:
    def __init__(self, root, store, *, timeout=120, verify_timeout=15, factory=None):
        self.root, self.store = root, store
        self.timeout, self.verify_timeout = timeout, verify_timeout
        self.factory = factory or pyatv.pair
        self.handler = self.task = self.timer = None
        self.state = None
        self.changed = lambda:None

    def snapshot(self): return dict(self.state) if self.state else None

    def busy(self, target=None):
        return bool(self.state and self.state['phase'] in ('connecting','pin','verifying') and
                    (target is None or target==self.state['target']))

    async def start(self, receiver, target):
        if self.busy(): raise UserError('Pairing is already in progress. Complete or cancel it.')
        if not receiver.get_service(Protocol.AirPlay): raise UserError('PIN pairing requires an AirPlay receiver.')
        await self.cancel(clear=True)
        self.state={'target':target,'name':receiver.name,'phase':'connecting','expiresAt':time.time()+self.timeout,'error':''}
        try:
            async with asyncio.timeout(self.verify_timeout):
                self.handler=await self.factory(copy.deepcopy(receiver),Protocol.AirPlay,asyncio.get_running_loop(),
                                                storage=MemoryStorage(),name='AirPlay MM')
                await self.handler.begin()
            self.state['phase']='pin'
            self.timer=asyncio.create_task(self._expire(self.state))
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception:
            self.state.update(phase='failed',error='PIN pairing could not be started; stored keys are preserved.')
            await self._close_handler()
            raise UserError(self.state['error']) from None
        return self.snapshot()

    async def _expire(self, state):
        await asyncio.sleep(max(0,state['expiresAt']-time.time()))
        self.timer=None
        if self.state is state and self.busy():
            self.state.update(phase='failed',error='Pairing timed out.')
            if self.task: self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True)
            await self._close_handler()

    async def finish(self, target, pin):
        if not self.busy(target) or self.state['phase']!='pin': raise UserError('No matching pairing is waiting for a PIN.')
        if not isinstance(pin,str) or not re.fullmatch(r'(?:[0-9]{4}|[0-9]{8}|[0-9]{3}-[0-9]{2}-[0-9]{3})',pin):
            raise UserError('Enter the displayed four- or eight-digit PIN.')
        if len(pin)==8: pin=pin[:3]+'-'+pin[3:5]+'-'+pin[5:]
        self.handler.pin(pin)
        self.state['phase']='verifying'
        self.task=asyncio.create_task(self._finish(self.state))
        return self.snapshot()

    async def _finish(self, state):
        try:
            async with asyncio.timeout(self.verify_timeout): await self.handler.finish()
            if not self.handler.has_paired: raise ValueError('PIN not confirmed')
            self.store.save_pyatv(state['target'],self.handler.service.credentials)
            state.update(phase='paired',error='')
            try: self.changed()
            except Exception: pass
        except asyncio.CancelledError: raise
        except Exception:
            state.update(phase='failed',error='Pairing failed or keys could not be saved. Existing pairings are preserved.')
        finally:
            if self.timer: self.timer.cancel(); self.timer=None
            await self._close_handler()

    async def _close_handler(self):
        handler,self.handler=self.handler,None
        if handler:
            try:
                async with asyncio.timeout(2): await handler.close()
            except Exception: pass

    async def cancel(self, *, clear=False):
        if self.busy(): self.state.update(phase='cancelled',error='Pairing cancelled.')
        tasks=(self.timer,self.task)
        self.timer=self.task=None
        for task in tasks:
            if task and task is not asyncio.current_task(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
        self.task=self.timer=None
        await self._close_handler()
        if clear: self.state=None
        return self.snapshot()
