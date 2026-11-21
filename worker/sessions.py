"""Bounded pool of own pyatv announcement sessions; no external sender."""
import asyncio
import time
from weakref import WeakValueDictionary
from pyatv_transport import PyatvSession as NativeSession


class SessionPool:
    def __init__(self, root, host, credentials, *, enabled=True, keep_seconds=300, factory=NativeSession):
        self.root, self.host, self.credentials = root, host, credentials
        self.enabled, self.keep_seconds, self.factory = enabled, keep_seconds, factory
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.target_locks = WeakValueDictionary()
        self.closing = False

    async def get(self, receiver, target, mode):
        lock = self.target_locks.setdefault(target, asyncio.Lock())
        async with lock:
            return await self._get(receiver, target, mode)

    async def _get(self, receiver, target, mode):
        retiring = []
        async with self.lock:
            if self.closing:
                raise RuntimeError('AirPlay connections are closing.')
            candidate = self.factory(self.root, self.host, receiver, target, mode, self.credentials.get(target))
            old = self.sessions.get(target)
            if old and old.live() and old.signature() == candidate.signature():
                return old
            if old:
                self.sessions.pop(target, None)
                retiring.append(old)
            while len(self.sessions) >= 8:
                idle = [(key, s) for key, s in self.sessions.items() if not s.live() or s.phase == 'idle']
                if not idle:
                    raise RuntimeError('Up to eight simultaneous AirPlay connections are supported.')
                key, session = min(idle, key=lambda row: row[1].last_used)
                self.sessions.pop(key, None)
                retiring.append(session)
            self.sessions[target] = candidate
        await asyncio.gather(*(s.close() for s in retiring))
        try:
            await candidate.start()
            if self.closing:
                await candidate.close()
                raise RuntimeError('AirPlay connections are closing.')
        except BaseException:
            async with self.lock:
                if self.sessions.get(target) is candidate:
                    self.sessions.pop(target, None)
            await candidate.close()
            raise
        return candidate

    def snapshot(self):
        return [{'target': k, 'name': s.receiver.name, 'address': str(s.receiver.address),
                 'state': s.phase, 'warm': s.used, 'idleSeconds': round(max(0, time.monotonic() - s.last_used))}
                for k, s in self.sessions.items() if s.live()]

    async def discard(self, target):
        async with self.lock:
            session = self.sessions.pop(target, None)
        if session:
            await session.close()

    async def close(self):
        self.closing = True
        await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
        self.sessions.clear()
