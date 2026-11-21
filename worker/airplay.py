"""Speech stays cached locally; verified upstream senders play to selected receivers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import ipaddress
import logging
from pathlib import Path
import time

import miniaudio
import pyatv
from pyatv.const import PairingRequirement, Protocol
from pyatv.helpers import is_device_supported
from pyatv.settings import AirPlayVersion
from pyatv.storage.memory_storage import MemoryStorage
from native import NativeSender, append_silence, prepend_silence
from pairing import Credentials, Pairing, redact
from sessions import SessionPool
from core import UserError
from device_metadata import receiver_metadata
from speech import SpeechService
from sayit_speech import SayItSpeechService
from cache_library import CacheLibrary
from speechify_speech import SpeechifyService, normalized_text, synthesize_with_fallback
from learning import LearningQueue
from speech import SpeechResult
from audio_gain import attenuate_pcm16, async_playback_wave, validate_gain
import os

LOG = logging.getLogger('airplay-mm')


class AirPlayBackend:
    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config
        self.master_gain = 100
        self.configs = {}
        self.device_rows = []
        self.storage = MemoryStorage()
        self.network = ipaddress.ip_network(config['network'], strict=False)
        self.presets = [(row['address'], row['name'], row.get('kind', 'speaker')) for row in config.get('presets', [])]
        enabled = config.get('airplayPreRollEnabled', True)
        configured_ms = config.get('airplayPreRollMs', 700)
        if not isinstance(enabled, bool) or isinstance(configured_ms, bool) or not isinstance(configured_ms, int) or not 0 <= configured_ms <= 2000:
            raise ValueError('Invalid AirPlay pre-roll')
        lead_ms = configured_ms if enabled else 0
        self.native = NativeSender(root, config['host'], lead_seconds=lead_ms / 1000)
        enabled = config.get('keepAliveEnabled', True)
        seconds = config.get('keepAliveSeconds', 300)
        if not isinstance(enabled, bool) or isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 3600:
            raise ValueError('Invalid persistent AirPlay connection settings')
        self.credentials = Credentials(root)
        self.pairing = Pairing(root, self.credentials)
        self.pairing.changed = self._pair_changed
        self.pool = SessionPool(root, config['host'], self.credentials, enabled=enabled, keep_seconds=seconds)
        self.provider = config.get('speechProvider', 'piper')
        self.speech = SayItSpeechService(root) if self.provider == 'sayit-google' else SpeechService(root, voice='de_DE-ramona-low' if self.provider == 'speechify' else config.get('piperVoice', 'de_DE-thorsten-medium'))
        self.catalog = CacheLibrary(root)
        self.online = SpeechifyService(root, self.catalog, api_key=os.environ.pop('AIRPLAY_SPEECHIFY_API_KEY', ''))
        self.learning = LearningQueue(root, self.online, enabled=config.get('alexaLearningEnabled', False))

    def set_master_gain(self, level):
        self.master_gain = validate_gain(level)

    async def start(self):
        try:
            await self.speech.start()
        finally:
            self.learning.start()

    async def close(self):
        await self.learning.close()
        await self.pairing.cancel()
        await self.pool.close()
        await self.speech.close()

    def connection_status(self):
        return self.pool.snapshot()

    def pairing_status(self):
        return self.pairing.snapshot()

    def pairing_busy(self, target):
        return self.pairing.busy(target)

    def _pair_changed(self):
        for row in self.device_rows:
            if row.get('pairable') and (self.credentials.get(row['id']) or {}).get('pyatv'):
                row.update(paired=True, available=True, reason='')

    async def pairing_command(self, action, data):
        target = data['target']
        if action == 'pairFinish':
            return await self.pairing.finish(target, data.get('pin'))
        if action == 'pairCancel':
            if self.pairing.busy() and not self.pairing.busy(target):
                raise UserError('The current pairing belongs to another receiver.')
            return await self.pairing.cancel(clear=True)
        if action == 'disconnect':
            await self.pool.discard(target)
            return {'disconnected': True}
        if action == 'forgetPairing':
            if self.pairing.busy(target):
                await self.pairing.cancel()
            await self.pool.discard(target)
            self.credentials.forget(target)
            return {'forgotten': True}
        if action != 'pairStart' or target not in self.configs:
            raise UserError('Receiver is no longer available; run discovery again.')
        await self.pool.discard(target)
        return await self.pairing.start(self.configs[target], target)

    def speech_stats(self):
        stats = self.speech.stats()
        if self.provider == 'speechify':
            stats = {**stats, **self.online.stats(), 'provider': 'speechify', 'voice': 'Runa · Speechify · Ramona fallback', 'onlineForNewTexts': True}
        return stats

    def extra_status(self):
        return {'learning': self.learning.snapshot(), 'speechify': self.online.stats()}

    async def cache_command(self, action, data):
        if action == 'cacheList':
            return self.catalog.listing(data)
        if action == 'cacheDelete':
            if not isinstance(data, dict) or set(data) != {'key'}:
                raise UserError('Invalid deletion request')
            if data['key'] in {self.online.active_key, self.learning.active}:
                raise UserError('This entry is currently being prepared')
            return self.catalog.delete(data['key'])
        if action == 'learn':
            return self.learning.add(data)
        if action == 'retryLearning':
            return self.learning.retry()
        if action == 'setSpeechifyKey':
            if not isinstance(data, dict) or set(data) != {'key'}:
                raise UserError('Invalid key transfer')
            self.online.update_key(data['key'])
            self.learning.retry()
            return {'saved': True, 'hasKey': bool(self.online.api_key)}
        raise UserError('Unknown cache action')

    def cached_audio(self, key):
        path, duration = self.catalog.audio(key)
        info = self.catalog.index.get(key, {})
        return SpeechResult(path, duration, True, 0, provider=info.get('provider', 'cache'), voice=info.get('voice', 'Previous voice'))

    async def discover(self):
        loop = asyncio.get_running_loop()
        searches = [pyatv.scan(loop, timeout=5, storage=self.storage)]
        if self.config.get('seedHosts'):
            searches.append(pyatv.scan(loop, hosts=self.config['seedHosts'], timeout=5, storage=self.storage))
        results = await asyncio.gather(*searches, return_exceptions=True)
        devices, configs = {}, {}
        for group in results:
            if isinstance(group, BaseException):
                LOG.warning('One discovery method failed: %s', group)
                continue
            for receiver in group:
                if receiver.address not in self.network or not receiver.identifier:
                    continue
                raop = receiver.get_service(Protocol.RAOP)
                airplay = receiver.get_service(Protocol.AirPlay)
                if not (airplay or raop) or (not airplay and not is_device_supported(receiver)):
                    continue
                # A newly named receiver at an existing IP does not inherit a saved target ID.
                identifier = receiver.identifier
                address = str(receiver.address)
                preset = next((p for p in self.presets if p[0] == address), None)
                service = airplay or raop
                stored = self.credentials.get(identifier)
                paired = bool(stored and stored.get('pyatv'))
                requirement = getattr(service, 'pairing', None)
                password = getattr(service, 'requires_password', False)
                available = not password and (paired or requirement not in {PairingRequirement.Mandatory, PairingRequirement.Disabled})
                pairable = bool(airplay and not password)
                devices[address] = {'id': identifier, 'name': preset[1] if preset else receiver.name,
                                    'advertisedName': receiver.name, 'address': address,
                                    'kind': preset[2] if preset else 'speaker', 'available': available,
                                    **receiver_metadata(receiver, airplay, raop),
                                    'reason': '' if available else 'AirPlay password required' if password else 'PIN pairing or receiver approval required',
                                    'pairable': pairable, 'paired': paired,
                                    'protocol': 'AirPlay', 'lastSeen': __import__('time').time()}
                configs[identifier] = receiver
        for address, name, kind in self.presets:
            devices.setdefault(address, {'id': 'offline-' + address, 'name': name, 'address': address,
                                         'kind': kind, 'available': False, 'reason': 'Unreachable', 'protocol': 'AirPlay'})
        if not configs and all(isinstance(x, BaseException) for x in results):
            raise RuntimeError('Both discovery methods failed')
        self.configs = configs
        self.device_rows = sorted(devices.values(), key=lambda d: (next((i for i, p in enumerate(self.presets) if p[0] == d['address']), 99), d['name']))
        return self.device_rows

    async def synthesize(self, text: str):
        text = normalized_text(text)
        fallback = ''
        if self.provider == 'speechify':
            result = await synthesize_with_fallback(self.online, self.speech, text)
            if result.provider == 'speechify':
                return result
            fallback = result.fallback_reason
        else:
            result = await self.speech.synthesize(text)
        result.provider = 'piper' if self.provider == 'speechify' else self.provider
        result.voice = 'Ramona' if self.provider == 'speechify' else self.speech.stats().get('voice', '')
        result.fallback_reason = fallback
        try:
            self.catalog.record(result, text, result.provider, result.voice)
        except (OSError, ValueError):
            LOG.warning('Cache description could not be saved; the audio file remains usable.')
        return result

    async def play(self, target: str, audio: Path, volume: float, duration: float, on_playing):
        receiver = self.configs.get(target)
        if not receiver:
            raise RuntimeError('Receiver is no longer available; run discovery again')
        mode = self.config.get('modes', {}).get(str(receiver.address), self.config.get('defaultMode', 'native-ntp'))
        if mode in {'native', 'native-ntp'}:
            errors = []
            def event(_target, state, error='', timing=None):
                if state == 'playing':
                    on_playing(timing)
                if state == 'failed':
                    errors.append(error)
            await self.play_many([target], audio, volume, duration, event)
            if errors:
                raise RuntimeError(errors[0])
            return
        return await self._legacy_play(receiver, audio, volume, duration, on_playing, mode)

    async def _legacy_play(self, receiver, audio, volume, duration, on_playing, mode):
        async with async_playback_wave(audio, self.root, getattr(self, 'master_gain', 100)) as playback:
            return await self._legacy_stream(receiver, playback, volume, duration, on_playing, mode)

    async def _legacy_stream(self, receiver, audio, volume, duration, on_playing, mode):
        stored=self.credentials.get(getattr(receiver,'identifier','')) if hasattr(self,'credentials') else None
        if stored and stored.get('pyatv'):
            receiver=deepcopy(receiver)
            for protocol in (Protocol.AirPlay,Protocol.RAOP):
                if receiver.get_service(protocol): receiver.set_credentials(protocol,stored['pyatv'])
        settings = await self.storage.get_settings(receiver)
        settings.protocols.raop.protocol_version = AirPlayVersion(mode)
        atv = await asyncio.wait_for(pyatv.connect(receiver, asyncio.get_running_loop(), protocol=Protocol.RAOP, storage=self.storage), 12)
        try:
            await atv.audio.set_volume(volume)
            # pyatv's stream_file awaits the whole stream. This callback marks the
            # streaming attempt, not an acoustic confirmation from the listener.
            on_playing()
            await asyncio.wait_for(atv.stream.stream_file(str(audio)), duration + 25)
        finally:
            tasks = atv.close()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                except Exception:
                    LOG.exception('Connection could not be fully closed')

    async def play_many(self, targets, audio, volume, duration, event):
        prepared, failures, legacy = {}, {}, {}
        sessions = {}
        started_at = {t: time.perf_counter() for t in targets}
        pcm = None
        try:
            native_targets = []
            for target in targets:
                receiver = self.configs.get(target)
                if not receiver:
                    failures[target] = 'Receiver is unreachable; other receivers will continue.'
                    continue
                mode = self.config.get('modes', {}).get(str(receiver.address), self.config.get('defaultMode', 'native-ntp'))
                if mode in {'native', 'native-ntp'}:
                    native_targets.append((target, receiver, mode))
                else:
                    legacy[target] = (receiver, mode)
            if native_targets:
                decoded = await asyncio.to_thread(miniaudio.decode_file, str(audio), nchannels=2, sample_rate=44100,
                                                 output_format=miniaudio.SampleFormat.SIGNED16)
                samples, level = decoded.samples.tobytes(), getattr(self, 'master_gain', 100)
                if level != 100:
                    samples = await asyncio.to_thread(attenuate_pcm16, samples, level)
                pcm = append_silence(prepend_silence(samples, self.native.lead_seconds), self.native.tail_seconds)

            async def prepare(target, receiver, mode):
                event(target, 'connecting')
                session = None
                try:
                    session = await self.pool.get(receiver, target, mode)
                    sessions[target] = session
                    prepared[target] = await session.prepare(pcm, volume)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Reconnect once only before any START. A failed warm flush cannot
                    # have played this new text; a failure after START is never retried.
                    retry = bool(session and session.used)
                    await self.pool.discard(target)
                    if retry:
                        try:
                            session = await self.pool.get(receiver, target, mode)
                            sessions[target] = session
                            prepared[target] = await session.prepare(pcm, volume)
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception as second:
                            error = second
                    failures[target] = redact(error)
                    await self.pool.discard(target)

            await asyncio.gather(*(prepare(*row) for row in native_targets))
            for target, error in failures.items():
                event(target, 'failed', error)
            at_ms = max([int(time.time() * 1000) + 750] +
                        [int(row['minimumStartMs']) + 50 for row in prepared.values()])

            async def native_play(target):
                session = sessions[target]
                try:
                    anchor = await session.begin(at_ms)
                    event(target, 'playing', timing={
                        'connectMs': round((time.perf_counter() - started_at[target]) * 1000, 2),
                        'receiverBufferMs': max(0, anchor - int(time.time() * 1000)),
                        'preRollMs': round(self.native.lead_seconds * 1000),
                        'reusedConnection': prepared[target]['reused'], 'startUnixMs': anchor,
                        'sender': 'pyatv', 'timingSource': 'senderTimeline'})
                    await session.finish(len(pcm) / 176400, self.pool.keep_seconds if self.pool.enabled else None)
                    event(target, 'completed')
                except asyncio.CancelledError:
                    event(target, 'cancelled', 'Announcement cancelled')
                    await self.pool.discard(target)
                    raise
                except Exception as error:
                    event(target, 'failed', redact(error))
                    await self.pool.discard(target)

            async def legacy_play(target, receiver, mode):
                try:
                    event(target, 'connecting')
                    await self._legacy_play(receiver, audio, volume, duration,
                                            lambda timing=None: event(target, 'playing', timing=timing), mode)
                    event(target, 'completed')
                except asyncio.CancelledError:
                    event(target, 'cancelled', 'Announcement cancelled')
                    raise
                except Exception as error:
                    event(target, 'failed', redact(error))

            await asyncio.gather(*(native_play(target) for target in prepared),
                                 *(legacy_play(target, *row) for target, row in legacy.items()))
        except BaseException:
            await asyncio.gather(*(self.pool.discard(t) for t in targets), return_exceptions=True)
            raise
