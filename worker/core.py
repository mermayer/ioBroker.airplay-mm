"""Bounded announcement queue. No ioBroker or device-specific code in this module."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import deque
import hashlib
import json
import logging
import math
from pathlib import Path
import time
from typing import Callable
import uuid
from audio_gain import validate_gain

LOG = logging.getLogger('airplay-mm')
VERSION = '0.1.0-beta.2'
TERMINAL = {'completed', 'partial', 'failed', 'cancelled', 'interrupted'}


class UserError(ValueError):
    pass


class PartialPlaybackError(RuntimeError):
    pass


def validate_targets(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 8 or
            any(not isinstance(x, str) or not 1 <= len(x) <= 128 or any(ord(c) < 32 for c in x) for x in value) or
            len(set(value)) != len(value)):
        raise UserError('Select one to eight different receivers')
    return list(value)


def validate_request(data: dict, max_volume: int = 40) -> dict:
    if not isinstance(data, dict) or set(data) - {'text', 'target', 'targets', 'volume', 'requestId'}:
        raise UserError('Invalid announcement')
    text = data.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise UserError('Enter a text with 1 to 500 characters')
    if any(ord(char) < 32 and char not in '\n\t' for char in text):
        raise UserError('Text contains prohibited control characters')
    targets = validate_targets(data['targets']) if 'targets' in data else None
    target = data.get('target', targets[0] if targets else None)
    if not isinstance(target, str) or not 1 <= len(target) <= 128:
        raise UserError('Select a receiver')
    if targets and target != targets[0]:
        raise UserError('Einzel- und Mehrfachauswahl widersprechen sich')
    volume = data.get('volume', 15)
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or not 0 <= volume <= max_volume:
        raise UserError(f'Announcement volume must be between 0 and {max_volume} percent')
    request_id = data.get('requestId') or str(uuid.uuid4())
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 96 or any(ord(c) < 33 for c in request_id):
        raise UserError('Invalid request ID')
    result = {'text': text.strip(), 'target': target, 'volume': volume, 'requestId': request_id}
    if targets:
        result['targets'] = targets
    return result


def atomic_json(path: Path, data) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


class Engine:
    def __init__(self, root: Path, backend, on_change: Callable | None = None, *, job_timeout: float = 90, max_volume: int = 40):
        self.root = root
        self.backend = backend
        self.on_change = on_change or (lambda state: None)
        self.job_timeout = job_timeout
        self.max_volume = max_volume
        self.queue = asyncio.Queue(maxsize=8)
        self.history = deque(maxlen=100)
        self.devices = []
        self.active = None
        self.active_task = None
        self.runner = None
        self.closing = False
        self.discovering = False
        self.last_error = ''
        self.started = time.time()
        self.settings = {'target': '', 'volume': min(15, max_volume)}
        self.discovery_lock = asyncio.Lock()
        self.precaching = False

    async def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for filename, default in [('history.json', []), ('settings.json', {'target': '', 'volume': 15})]:
            path = self.root / filename
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > (2_000_000 if filename == 'history.json' else 500_000):
                    raise ValueError('File too large')
                value = json.loads(path.read_text(encoding='utf-8'))
                if filename == 'history.json':
                    if not isinstance(value, list):
                        raise ValueError('Invalid history')
                    for row in value[-100:]:
                        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not isinstance(row.get('status'), str):
                            continue
                        if row['status'] not in TERMINAL:
                            row.update(status='interrupted', error='Interrupted by restart; no automatic replay', finished=time.time())
                            self._finish_targets(row, 'interrupted', row['error'])
                        self.history.append(row)
                elif isinstance(value, dict):
                    volume = value.get('volume', 15)
                    if isinstance(volume, (int, float)) and not isinstance(volume, bool) and math.isfinite(volume) and 0 <= volume <= self.max_volume:
                        self.settings['volume'] = volume
                    if 'masterGain' in value:
                        try:
                            self.settings['masterGain'] = validate_gain(value['masterGain'])
                        except ValueError:
                            LOG.warning('Invalid saved master gain was ignored; using the original level.')
                    if isinstance(value.get('target'), str):
                        self.settings['target'] = value['target'][:128]
                    if 'targets' in value:
                        try:
                            self.settings['targets'] = validate_targets(value['targets'])
                            self.settings['target'] = self.settings['targets'][0]
                        except UserError:
                            LOG.warning('Invalid saved receiver group was ignored.')
            except (OSError, ValueError, TypeError) as exc:
                self.last_error = f'{filename} could not be read: {exc}'
                LOG.warning(self.last_error)
                path.replace(path.with_name(path.name + '.unlesbar-' + str(time.time_ns())))
        self._persist()
        if hasattr(self.backend, 'start'):
            try:
                await self.backend.start()
            except Exception as error:
                self.last_error = 'Speech provider could not be prepared: ' + str(error)[:800]
                LOG.exception(self.last_error)
        self.runner = asyncio.create_task(self._run(), name='announcement-queue')
        self._emit()

    def snapshot(self):
        speech = self.backend.speech_stats() if hasattr(self.backend, 'speech_stats') else None
        history, budget = [], 0
        for row in list(reversed(self.history))[:30]:
            encoded = json.dumps(row, ensure_ascii=True)
            if budget + len(encoded) > 200_000:
                break
            history.append(json.loads(encoded))
            budget += len(encoded)
        return {'version': VERSION, 'ready': not self.closing, 'busy': self.active is not None,
                'discovering': self.discovering, 'queued': self.queue.qsize(),
                'active': deepcopy(self.active), 'devices': deepcopy(self.devices),
                'history': history, 'settings': deepcopy(self.settings),
                'lastError': self.last_error, 'maxVolume': self.max_volume, 'masterGain': self.settings.get('masterGain', 100), 'voice': speech.get('voice') if speech else 'Thorsten · German · Local',
                'uptimeSeconds': round(time.time() - self.started),
                'speech': speech,
                'preparingText': self.precaching,
                'connections': self.backend.connection_status() if hasattr(self.backend, 'connection_status') else [],
                'pairing': self.backend.pairing_status() if hasattr(self.backend, 'pairing_status') else None,
                **(self.backend.extra_status() if hasattr(self.backend, 'extra_status') else {})}

    async def cache_command(self, action, data=None):
        if not hasattr(self.backend, 'cache_command'):
            raise UserError('Cache management is not available yet')
        if action == 'cacheDelete' and isinstance(data, dict):
            key = data.get('key')
            if any(row.get('cacheKey') == key and row.get('status') not in TERMINAL for row in self.history):
                raise UserError('This cache entry is in use by an announcement')
        result = await self.backend.cache_command(action, data)
        self._emit()
        return result

    async def play_cached(self, data):
        if not isinstance(data, dict) or set(data) - {'key', 'target', 'targets', 'volume', 'requestId'} or 'key' not in data:
            raise UserError('Invalid cache playback request')
        audio = self.backend.cached_audio(data['key'])
        info = self.backend.catalog.index.get(data['key'], {})
        return await self.submit({**{k: v for k, v in data.items() if k != 'key'}, 'text': info.get('text', 'Cached announcement')}, cached_key=data['key'])

    async def precache(self, data):
        if self.closing or not isinstance(data, dict) or set(data) != {'text'}:
            raise UserError('Invalid text preparation request')
        text = data['text']
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise UserError('Enter a text with 1 to 500 characters')
        if any(ord(char) < 32 and char not in '\n\t' for char in text):
            raise UserError('Text contains prohibited control characters')
        if self.precaching:
            raise UserError('A text is already being prepared')
        self.precaching = True
        self._emit()
        try:
            result = await asyncio.wait_for(self.backend.synthesize(text.strip()), 30)
            self.last_error = ''
            return {'prepared': True, 'durationSeconds': round(result[1] if isinstance(result, tuple) else result.duration, 2),
                    'cacheHit': bool(getattr(result, 'cache_hit', False)), 'generationMs': getattr(result, 'generation_ms', None),
                    'voice': getattr(result, 'voice', ''), 'fallbackReason': getattr(result, 'fallback_reason', '')}
        except Exception as error:
            self.last_error = 'Text could not be prepared: ' + type(error).__name__ + ': ' + str(error)[:800]
            LOG.exception(self.last_error)
            raise
        finally:
            self.precaching = False
            self._emit()

    def _persist(self):
        try:
            atomic_json(self.root / 'history.json', list(self.history))
            atomic_json(self.root / 'settings.json', self.settings)
            return True
        except OSError as exc:
            self.last_error = 'History could not be saved: ' + str(exc)[:500]
            LOG.exception(self.last_error)
            return False

    def _emit(self):
        try:
            self.on_change(self.snapshot())
        except Exception:
            LOG.exception('Status callback failed')

    async def discover(self):
        async with self.discovery_lock:
            if self.closing:
                raise UserError('Speech output is stopping')
            self.discovering = True
            self._emit()
            try:
                self.devices = await asyncio.wait_for(self.backend.discover(), 15)
                if not self.settings['target']:
                    first = next((d for d in self.devices if d.get('available')), None)
                    if first:
                        self.settings['target'] = first['id']
                        self._persist()
                return self.devices
            except Exception as exc:
                self.last_error = 'Receiver discovery failed: ' + str(exc)[:500]
                LOG.warning(self.last_error)
                raise
            finally:
                self.discovering = False
                self._emit()

    async def submit(self, data: dict, *, cached_key=None):
        if self.closing:
            raise UserError('Speech output is stopping')
        request = validate_request(data, self.max_volume)
        fingerprint = hashlib.sha256(json.dumps({**{k: v for k, v in request.items() if k != 'requestId'}, **({'cacheKey': cached_key} if cached_key else {})}, sort_keys=True).encode()).hexdigest()
        for row in self.history:
            if row.get('requestId') == request['requestId']:
                if row.get('fingerprint') != fingerprint:
                    raise UserError('Request ID was already used for another announcement')
                return dict(row)
        targets = request.get('targets', [request['target']])
        selected = [next((d for d in self.devices if d['id'] == target), None) for target in targets]
        if any(d is None for d in selected) or not any(d.get('available') for d in selected):
            raise UserError('The selected receiver is unreachable or requires pairing')
        if hasattr(self.backend, 'pairing_busy') and any(self.backend.pairing_busy(t) for t in targets):
            raise UserError('Pairing is in progress for a selected receiver')
        if self.queue.full():
            raise UserError('The queue is full; wait a moment')
        row = {**request, **({'cacheKey': cached_key} if cached_key else {}), 'id': str(uuid.uuid4()), 'fingerprint': fingerprint, 'targetName': ', '.join(d['name'] for d in selected),
               'status': 'queued', 'created': time.time(), 'error': ''}
        if len(targets) > 1:
            row['targetResults'] = [{'target': target, 'name': d['name'], 'status': 'queued', 'error': ''}
                                    for target, d in zip(targets, selected)]
        previous_settings = self.settings.copy()
        self.history.append(row)
        self.settings.update(target=request['target'], volume=request['volume'])
        if 'targets' in request:
            self.settings['targets'] = list(request['targets'])
        else:
            self.settings.pop('targets', None)
        if not self._persist():
            self.history.pop()
            self.settings = previous_settings
            self._emit()
            raise UserError('Announcement could not be saved safely; check available disk space')
        self.queue.put_nowait(row)
        self._emit()
        return dict(row)

    def update_settings(self, data: dict):
        if self.closing:
            raise UserError('Speech output is stopping')
        if not isinstance(data, dict) or not data or set(data) - {'target', 'targets', 'volume', 'masterGain'}:
            raise UserError('Invalid receiver setting')
        trial = {**self.settings, **data}
        if 'targets' in data:
            trial['targets'] = validate_targets(data['targets'])
            if 'target' in data and data['target'] != trial['targets'][0]:
                raise UserError('Einzel- und Mehrfachauswahl widersprechen sich')
            trial['target'] = trial['targets'][0]
        elif 'target' in data:
            trial.pop('targets', None)
        if 'target' in data and (not isinstance(trial['target'], str) or not 1 <= len(trial['target']) <= 128):
            raise UserError('Invalid receiver ID')
        volume = trial['volume']
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or not 0 <= volume <= self.max_volume:
            raise UserError('Invalid announcement volume')
        if 'masterGain' in trial:
            try:
                validate_gain(trial['masterGain'])
            except ValueError as error:
                raise UserError(str(error)) from None
        previous = self.settings
        self.settings = trial
        if not self._persist():
            self.settings = previous
            self._emit()
            raise UserError('The setting could not be saved safely')
        self._emit()
        return deepcopy(self.settings)

    async def pairing_command(self, action, data):
        if self.closing or not isinstance(data, dict):
            raise UserError('Invalid pairing request')
        allowed = {'target', 'pin'} if action == 'pairFinish' else {'target'}
        if set(data) - allowed or not isinstance(data.get('target'), str):
            raise UserError('Select a receiver')
        target = data['target']
        current_pairing = self.backend.pairing_status() if hasattr(self.backend, 'pairing_status') else None
        belongs_to_pairing = bool(current_pairing and current_pairing.get('target') == target and action in {'pairCancel', 'pairFinish'})
        if not belongs_to_pairing and not any(d['id'] == target for d in self.devices):
            raise UserError('Receiver is no longer available; run discovery again')
        if any(row.get('status') not in TERMINAL and target in row.get('targets', [row['target']])
               for row in self.history):
            raise UserError('An announcement is active or queued for this receiver')
        if not hasattr(self.backend, 'pairing_command'):
            raise UserError('This output mode does not support pairing')
        result = await self.backend.pairing_command(action, data)
        if action == 'forgetPairing':
            await self.discover()
        self._emit()
        return result

    def _phase(self, row, phase):
        row['status'] = phase
        self._persist()
        self._emit()

    @staticmethod
    def _finish_targets(row, status, error):
        for detail in row.get('targetResults', []):
            if detail.get('status') not in TERMINAL:
                detail.update(status=status, error=error)

    async def _perform(self, row):
        self._phase(row, 'generating')
        began = time.perf_counter()
        result = self.backend.cached_audio(row['cacheKey']) if row.get('cacheKey') else await self.backend.synthesize(row['text'])
        audio, duration = result
        if hasattr(result, 'path'):
            row['cacheKey'] = result.path.stem
        row['generationMs'] = round((time.perf_counter() - began) * 1000, 2)
        if hasattr(result, 'cache_hit'):
            row['cacheHit'] = result.cache_hit
        if hasattr(self.backend, 'speech_stats'):
            row['speechProvider'] = self.backend.speech_stats().get('provider', 'piper')
        if getattr(result, 'provider', ''):
            row['speechProvider'] = result.provider
        if getattr(result, 'voice', ''):
            row['voice'] = result.voice
        if getattr(result, 'fallback_reason', ''):
            row['fallbackReason'] = result.fallback_reason
        row['durationSeconds'] = round(duration, 2)
        row['masterGain'] = self.settings.get('masterGain', 100)
        if hasattr(self.backend, 'set_master_gain'):
            self.backend.set_master_gain(row['masterGain'])
        elif row['masterGain'] != 100:
            raise UserError('This output mode does not support master gain')
        self._phase(row, 'connecting')
        connecting = time.perf_counter()
        def playing(timing=None):
            row['connectMs'] = round((time.perf_counter() - connecting) * 1000, 2)
            if isinstance(timing, dict):
                if len(row.get('targets', [row['target']])) == 1 and isinstance(timing.get('reusedConnection'), bool):
                    row['reusedConnection'] = timing['reusedConnection']
                for name in ['receiverBufferMs', 'preRollMs']:
                    value = timing.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 30000:
                        row[name] = round(value, 2)
                if 'receiverBufferMs' in row and 'preRollMs' in row:
                    row['estimatedStartMs'] = round(max(0, (time.time() - row['created']) * 1000) + row['receiverBufferMs'] + row['preRollMs'], 2)
            self._phase(row, 'playing')
        targets = row.get('targets', [row['target']])
        if len(targets) == 1:
            await self.backend.play(row['target'], audio, row['volume'], duration, playing)
            return

        def target_event(target, status, error='', timing=None):
            detail = next(item for item in row['targetResults'] if item['target'] == target)
            detail.update(status=status, error=error)
            if isinstance(timing, dict):
                detail.update({key: value for key, value in timing.items()
                               if key in {'connectMs', 'receiverBufferMs', 'preRollMs', 'reusedConnection', 'startUnixMs'}})
            if status == 'playing':
                playing(timing)
            else:
                self._persist()
                self._emit()

        if hasattr(self.backend, 'play_many'):
            await self.backend.play_many(targets, audio, row['volume'], duration, target_event)
        else:
            async def one(target):
                try:
                    target_event(target, 'connecting')
                    await self.backend.play(target, audio, row['volume'], duration,
                                            lambda timing=None: target_event(target, 'playing', timing=timing))
                    target_event(target, 'completed')
                except asyncio.CancelledError:
                    target_event(target, 'cancelled', 'Announcement cancelled')
                    raise
                except Exception as error:
                    target_event(target, 'failed', str(error)[:600])
            await asyncio.gather(*(one(t) for t in targets))
        failures = [item for item in row['targetResults'] if item['status'] != 'completed']
        if failures:
            message = '; '.join(item['name'] + ': ' + (item['error'] or item['status']) for item in failures)[:1000]
            if len(failures) != len(targets):
                raise PartialPlaybackError(message)
            raise RuntimeError(message)

    async def _run(self):
        while not self.closing:
            row = await self.queue.get()
            self.active = row
            row['started'] = time.time()
            self.active_task = asyncio.create_task(self._perform(row))
            try:
                await asyncio.wait_for(self.active_task, self.job_timeout)
                row['status'] = 'completed'
                row['error'] = ''
                self.last_error = ''
                LOG.info('Announcement %s to %s completed', row['id'], row['targetName'])
            except asyncio.CancelledError:
                row['status'] = 'interrupted' if self.closing else 'cancelled'
                row['error'] = 'Durch Neustart unterbrochen' if self.closing else 'Announcement cancelled'
                self._finish_targets(row, row['status'], row['error'])
            except PartialPlaybackError as exc:
                row['status'] = 'partial'
                row['error'] = str(exc)[:1000]
                self.last_error = row['error']
                LOG.warning('Announcement %s partially delivered: %s', row['id'], row['error'])
            except Exception as exc:
                row['status'] = 'failed'
                row['error'] = ('Announcement timed out' if isinstance(exc, TimeoutError) else type(exc).__name__ + ': ' + str(exc))[:1000]
                self.last_error = row['error']
                self._finish_targets(row, 'failed', row['error'])
                LOG.exception('Announcement %s to %s failed: %s', row['id'], row['targetName'], row['error'])
            finally:
                row['finished'] = time.time()
                self.active = None
                self.active_task = None
                self.queue.task_done()
                self._persist()
                self._emit()

    async def cancel(self):
        count = 0
        if self.active_task and not self.active_task.done():
            self.active_task.cancel()
            count += 1
        while not self.queue.empty():
            row = self.queue.get_nowait()
            row.update(status='cancelled', error='Announcement cancelled', finished=time.time())
            self._finish_targets(row, 'cancelled', row['error'])
            self.queue.task_done()
            count += 1
        self._persist()
        self._emit()
        return count

    async def close(self):
        if self.closing:
            return
        self.closing = True
        await self.cancel()
        if self.active_task:
            await asyncio.gather(self.active_task, return_exceptions=True)
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        if hasattr(self.backend, 'close'):
            try:
                await self.backend.close()
            except Exception:
                LOG.exception('Local speech process could not be stopped cleanly')
        self._persist()
        self._emit()
