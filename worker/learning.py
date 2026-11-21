"""Persistent, bounded background preparation. This module cannot play audio."""
import asyncio
import json
import logging
import time
from speechify_speech import normalized_text

LOG = logging.getLogger('airplay-mm')


class LearningQueue:
    def __init__(self, root, service, enabled=False):
        self.path, self.service, self.enabled = root / 'learning.json', service, enabled
        self.pending, self.failed, self.completed, self.last_error = [], [], 0, ''
        self.wake, self.task, self.closing, self.active = asyncio.Event(), None, False, None
        if self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size < 1_000_000:
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                for name, limit in [('pending', 256), ('failed', 128)]:
                    rows = saved.get(name, [])
                    if not isinstance(rows, list):
                        continue
                    for row in rows[:limit]:
                        try:
                            text = normalized_text(row['text'])
                            getattr(self, name).append({'text': text, 'tries': min(3, max(0, int(row.get('tries', 0)))),
                                                       'notBefore': min(time.time()+300, float(row.get('notBefore', 0)))})
                        except (ValueError, TypeError, KeyError):
                            continue
                self.completed = max(0, int(saved.get('completed', 0)))
            except (OSError, ValueError, TypeError, AttributeError):
                LOG.warning('Learning queue cannot be read; existing speech files are preserved.')

    def save(self):
        temp = self.path.with_suffix('.tmp')
        if self.path.is_symlink() or temp.is_symlink():
            raise ValueError('Unexpected link in learning queue')
        temp.write_text(json.dumps({'pending': self.pending, 'failed': self.failed, 'completed': self.completed}, ensure_ascii=False), encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(self.path)

    def snapshot(self):
        return {'enabled': self.enabled, 'pending': len(self.pending), 'failed': len(self.failed),
                'completed': self.completed, 'active': bool(self.active), 'hasKey': bool(self.service.api_key),
                'lastError': self.last_error}

    def add(self, data):
        if not self.enabled:
            raise ValueError('Alexa learning mode is disabled')
        if not isinstance(data, dict) or set(data) != {'text'}:
            raise ValueError('Invalid learning request')
        text = normalized_text(data['text'])
        if self.service.cache.lookup(self.service.key(text)):
            return {'cached': True}
        if any(row['text'] == text for row in self.pending + self.failed):
            return {'alreadyQueued': True}
        if len(self.pending) >= 256:
            self.last_error = 'Learning queue is full (256 texts). Alexa continues unchanged.'
            raise ValueError(self.last_error)
        self.pending.append({'text': text, 'tries': 0, 'notBefore': 0})
        try:
            self.save()
        except (OSError, ValueError):
            self.pending.pop()
            raise
        self.wake.set()
        return {'queued': True}

    def retry(self):
        room = 256 - len(self.pending)
        moved, self.failed = self.failed[:room], self.failed[room:]
        for row in moved:
            row.update(tries=0, notBefore=0)
        self.pending.extend(moved)
        self.service.retry_at, self.last_error = 0, ''
        self.save()
        self.wake.set()
        return {'retried': len(moved)}

    def start(self):
        if self.enabled and not self.task:
            self.task = asyncio.create_task(self.run())

    async def wait(self, seconds=3):
        try:
            await asyncio.wait_for(self.wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    async def run(self):
        while not self.closing:
            if not self.pending or not self.service.api_key:
                await self.wait()
                continue
            row = self.pending[0]
            if max(row['notBefore'], self.service.retry_at) > time.time():
                await self.wait()
                continue
            self.active = self.service.key(row['text'])
            try:
                await self.service.synthesize(row['text'])
                self.pending.remove(row)
                self.completed += 1
                self.last_error = ''
            except asyncio.CancelledError:
                raise  # Unfinished preparation stays persisted for the next start.
            except Exception as error:
                self.last_error = str(error)[:500]
                LOG.warning('Speechify learning: %s. Alexa is unchanged.', self.last_error)
                row['tries'] += 1
                if row['tries'] >= 3 or getattr(error, 'permanent', False):
                    self.pending.remove(row)
                    self.failed.append(row)
                    self.failed = self.failed[-128:]
                else:
                    row['notBefore'] = time.time() + max(10, getattr(error, 'retry_after', 30))
            finally:
                self.active = None
                try:
                    self.save()
                except (OSError, ValueError):
                    LOG.error('Learning progress could not be saved; existing audio files are preserved.')

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
