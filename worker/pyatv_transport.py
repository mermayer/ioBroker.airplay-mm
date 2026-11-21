"""Own announcement scheduling on unmodified pyatv 0.18.0 (MIT).

Persistent audio transport using the pyatv RAOP client.
pyatv owns RTSP, authentication, RTP encryption, retransmits and NTP. This
adapter owns a continuous PCM source, per-announcement scheduling and cleanup.
The internal pyatv streaming API is deliberately pinned and tested below.
"""
from __future__ import annotations

import asyncio
from array import array
import math
import sys
import time

from pyatv.auth.hap_pairing import parse_credentials
from pyatv.const import PairingRequirement, Protocol
from pyatv.protocols.airplay.auth import extract_credentials
from pyatv.protocols.airplay.utils import AirPlayMajorVersion, get_protocol_version, pct_to_dbfs
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.protocols.raop.protocols import StreamContext, airplayv1, airplayv2
from pyatv.protocols.raop.stream_client import StreamClient, AudioProtocol
from pyatv.settings import AirPlayVersion, Settings
from pyatv.support.http import HttpConnection
from pyatv.support.metadata import EMPTY_METADATA
from pyatv.support.rtsp import RtspSession

RATE = 44100
FRAME_BYTES = 4
NTP_EPOCH = 2208988800


class AnnouncementSource(AudioSource):
    """Always supplies silence between clips; a clip is consumed exactly once.

    The source follows the RTP sender's sample clock, rather than a second wall
    clock loop. Its PCM is network byte order, as required by pyatv's transport.
    No filesystem or network access is performed by this class.
    """
    def __init__(self, context):
        self.context = context
        self.first_read = asyncio.Event()
        self.consumed = asyncio.Event()
        self.closed = False
        self.pcm = None
        self.start_frame = None
        self.sent_bytes = 0

    @property
    def sample_rate(self): return RATE

    @property
    def channels(self): return 2

    @property
    def sample_size(self): return 2

    @property
    def duration(self): return 0

    async def get_metadata(self): return EMPTY_METADATA

    def next_audible_ms(self):
        return ((self.context.head_ts + self.context.latency) / RATE - NTP_EPOCH) * 1000

    def prepare(self, pcm):
        if self.closed or self.pcm is not None:
            raise RuntimeError('Audio stream is closed or still in use.')
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % FRAME_BYTES or len(pcm) > 64_000_000:
            raise ValueError('Invalid or oversized PCM announcement.')
        samples = array('h')
        samples.frombytes(pcm)
        if sys.byteorder == 'little': samples.byteswap()
        self.pcm = samples.tobytes()
        self.start_frame = None
        self.sent_bytes = 0
        self.consumed.clear()

    def schedule(self, audible_unix_ms):
        if self.closed or self.pcm is None or self.start_frame is not None:
            raise RuntimeError('No prepared or started announcement.')
        # One shared audible Unix time is mapped onto each receiver's RTP clock.
        self.start_frame = round((audible_unix_ms / 1000 + NTP_EPOCH) * RATE) - self.context.latency
        if self.start_frame < self.context.head_ts:
            raise RuntimeError('The scheduled start is behind the audio stream.')

    async def readframes(self, nframes):
        if self.closed: return b''
        self.first_read.set()
        output = bytearray(nframes * FRAME_BYTES)
        if self.pcm is not None and self.start_frame is not None:
            frame = self.context.head_ts
            end = frame + nframes
            clip_end = self.start_frame + len(self.pcm) // FRAME_BYTES
            first, last = max(frame, self.start_frame), min(end, clip_end)
            if first < last:
                offset, length = (first - self.start_frame) * FRAME_BYTES, (last - first) * FRAME_BYTES
                output[(first-frame)*FRAME_BYTES:(last-frame)*FRAME_BYTES] = self.pcm[offset:offset+length]
                self.sent_bytes += length
            if end >= clip_end:
                self.consumed.set()
        return bytes(output)

    def release(self):
        if not self.consumed.is_set(): raise RuntimeError('Audio was not fully delivered to the sender.')
        self.pcm = self.start_frame = None

    async def close(self):
        self.closed = True
        self.pcm = self.start_frame = None


def credentials_for(service, saved):
    if saved and saved.get('pyatv'):
        return parse_credentials(saved['pyatv'])
    if saved and saved.get('credentials') and getattr(service, 'pairing', None) == PairingRequirement.Mandatory:
        raise RuntimeError('Existing pairing is preserved. Pair once by PIN for the new sender.')
    return extract_credentials(service)


class ObservedAirPlay2(airplayv2.AirPlayV2):
    def __init__(self, context, rtsp, owner):
        super().__init__(context, rtsp)
        self.owner = owner

    async def setup(self, timing_server_port, control_client_port):
        self.owner.track_timing()
        self.owner.connect_stage = 'session_setup'
        await super().setup(timing_server_port, control_client_port)

    async def setup_audio_stream(self, control_client_port):
        self.owner.connect_stage = 'audio_setup'
        await super().setup_audio_stream(control_client_port)


class PyatvSession:
    """One authenticated, continuous pyatv stream per receiver.

    begin reports a planned sender timestamp, not an acoustic or receiver
    acknowledgment. No clip is retried after it has been scheduled.
    """
    def __init__(self, root, local_address, receiver, target, mode, credential=None, *, timeout=22):
        self.root, self.local_address, self.receiver = root, local_address, receiver
        self.target, self.mode, self.credential, self.timeout = target, mode, credential, timeout
        self.context = StreamContext()
        self.source = AnnouncementSource(self.context)
        self.connection = self.client = self.protocol = None
        self.stream_task = self.idle_task = self.close_task = None
        self.audio_transport = None
        self.closed = self.used = self.connected = False
        self.phase, self.error = 'connecting', ''
        self.last_used = time.monotonic()
        self.anchor = 0
        self.started = False
        self.connect_stage = 'tcp'
        self.timing_requests = self.timing_port = 0
        self.timing_server = self.timing_callback = None

    def track_timing(self):
        server = self.client.timing_server
        self.timing_server, self.timing_callback = server, server.datagram_received
        self.timing_port = server.port
        original = self.timing_callback
        def receive(data, address):
            if address[0] == str(self.receiver.address) and len(data) >= 32 and data[1] & 0x7f == 0x52:
                self.timing_requests += 1
            return original(data, address)
        server.datagram_received = receive

    def connection_error(self, error):
        if isinstance(error, TimeoutError) and self.connect_stage == 'audio_setup' and not self.timing_requests and self.timing_port:
            return ('AirPlay audio setup timed out: no UDP timing requests received from the receiver. '
                    f'Allow inbound UDP timing at {self.local_address}:{self.timing_port} '
                    'in the local firewall or VPN.')
        return f'AirPlay connection failed ({self.connect_stage}, {type(error).__name__}).'

    def service(self):
        if self.mode == '1': return self.receiver.get_service(Protocol.RAOP)
        return self.receiver.get_service(Protocol.AirPlay) or self.receiver.get_service(Protocol.RAOP)

    def signature(self):
        service = self.service()
        return (str(self.receiver.address), service.port if service else None, self.mode, self.credential)

    async def start(self):
        try:
            await self._start()
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            self.error = self.connection_error(error)
            await self.close()
            raise RuntimeError(self.error) from None

    async def _start(self):
        service = self.service()
        if service is None: raise RuntimeError('Receiver does not provide a compatible AirPlay service.')
        preference = AirPlayVersion.V1 if self.mode == '1' else AirPlayVersion.V2 if self.mode == '2' else AirPlayVersion.Auto
        version = get_protocol_version(service, preference)
        self.context.credentials = credentials_for(service, self.credential)
        self.context.password = getattr(service, 'password', None)
        async with asyncio.timeout(self.timeout):
            loop = asyncio.get_running_loop()
            options = {'local_addr': (self.local_address, 0)} if self.local_address else {}
            _, self.connection = await loop.create_connection(HttpConnection, str(self.receiver.address), service.port, **options)
            rtsp = RtspSession(self.connection)
            self.protocol = (ObservedAirPlay2(self.context, rtsp, self) if version == AirPlayMajorVersion.AirPlayV2
                             else airplayv1.AirPlayV1(self.context, rtsp))
            self.client = StreamClient(rtsp, self.context, self.protocol, Settings())
            await self.client.initialize(service.properties)
            self.connect_stage = 'stream_start'
            if (self.context.sample_rate, self.context.channels, self.context.bytes_per_channel) != (RATE, 2, 2):
                raise RuntimeError('Receiver requires an audio format other than 44.1 kHz stereo PCM16.')
            await self.client.set_volume(pct_to_dbfs(0))
            self.stream_task = asyncio.create_task(self._stream())
            ready = asyncio.create_task(self.source.first_read.wait())
            try:
                done, _ = await asyncio.wait((self.stream_task, ready), return_when=asyncio.FIRST_COMPLETED)
                if self.stream_task in done:
                    raise RuntimeError(self.error or 'Receiver did not start the audio stream.')
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
            if self.closed:
                raise RuntimeError('AirPlay connection was cancelled.')
            self.connected = True
            self.phase = 'idle'

    async def _stream(self):
        try:
            # Own the UDP lifetime explicitly. pyatv 0.18.0's send_audio teardown
            # may fail on a lost RTSP socket before closing its local UDP socket.
            # The packet engine below is still the unmodified MIT library.
            self.context.reset()
            self.audio_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                AudioProtocol, local_addr=(self.connection.local_ip,0),
                remote_addr=(self.connection.remote_ip,self.context.server_port))
            self.client.control_client.start(self.connection.remote_ip)
            await self.protocol.start_feedback()
            await self.client.rtsp.record()
            await self.client.rtsp.flush(headers={
                'Range':'npt=0-', 'Session':self.context.rtsp_session,
                'RTP-Info':f'seq={self.context.rtpseq};rtptime={self.context.rtptime}'})
            await self.client._stream_data(self.source,self.audio_transport)
            if not self.closed: self.error = 'AirPlay audio stream ended unexpectedly.'
        except asyncio.CancelledError:
            raise
        except Exception:
            # Authentication exceptions can embed keys; never forward raw text.
            self.error = 'AirPlay audio stream failed; no automatic replay.'
        finally:
            try:
                async with asyncio.timeout(1): await self.client.rtsp.teardown(self.context.rtsp_session)
            except (Exception,asyncio.CancelledError): pass
            finally:
                if self.audio_transport: self.audio_transport.close()
                self.client.close()
                self.connection.close()
            self.connected = False
            if not self.closed: self.phase = 'failed'

    def live(self):
        return bool(not self.closed and self.connected and self.stream_task and not self.stream_task.done()
                    and self.connection and self.connection.transport and not self.connection.transport.is_closing())

    async def prepare(self, pcm, volume):
        if not self.live(): raise RuntimeError(self.error or 'AirPlay connection is unavailable.')
        if self.phase != 'idle': raise RuntimeError('AirPlay connection is already in use.')
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or not 0 <= volume <= 100:
            raise ValueError('Invalid AirPlay volume.')
        if self.idle_task:
            self.idle_task.cancel()
            await asyncio.gather(self.idle_task, return_exceptions=True)
            self.idle_task = None
        async with asyncio.timeout(self.timeout): await self.client.set_volume(pct_to_dbfs(volume))
        self.source.prepare(pcm)
        self.started = False
        self.phase = 'prepared'
        return {'reused': self.used, 'minimumStartMs': math.ceil(max(time.time()*1000+1750, self.source.next_audible_ms()+250))}

    async def begin(self, at_ms):
        if not self.live() or self.phase != 'prepared': raise RuntimeError('No prepared AirPlay connection.')
        if isinstance(at_ms, bool) or not isinstance(at_ms, int) or not time.time()*1000-1000 <= at_ms <= time.time()*1000+15000:
            raise ValueError('Invalid AirPlay start time.')
        minimum = math.ceil(self.source.next_audible_ms()+100)
        if at_ms < minimum: raise RuntimeError('Shared start time has passed; the announcement will not be replayed.')
        self.source.schedule(at_ms)
        self.anchor, self.started, self.phase = at_ms, True, 'playing'
        return self.anchor

    async def finish(self, duration, keep_seconds):
        if not self.started: raise RuntimeError('Announcement was not started.')
        deadline = time.monotonic() + max(0, self.anchor/1000-time.time()) + duration + .75
        while time.monotonic() < deadline:
            if not self.live(): raise RuntimeError(self.error or 'AirPlay connection was lost before the announcement finished.')
            await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))
        if not self.live(): raise RuntimeError(self.error or 'AirPlay connection was lost before the announcement finished.')
        self.source.release()
        self.used, self.phase, self.last_used = True, 'idle', time.monotonic()
        if keep_seconds is None: await self.close()
        elif keep_seconds: self.idle_task = asyncio.create_task(self._expire(keep_seconds))

    async def _expire(self, seconds):
        await asyncio.sleep(seconds)
        self.idle_task = None
        await self.close()

    async def close(self):
        if self.close_task is None:
            self.closed, self.connected = True, False
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.idle_task:
            self.idle_task.cancel()
            await asyncio.gather(self.idle_task, return_exceptions=True)
            self.idle_task = None
        await self.source.close()
        if self.stream_task:
            self.stream_task.cancel()
            try:
                async with asyncio.timeout(2): await asyncio.gather(self.stream_task, return_exceptions=True)
            except TimeoutError:
                self.error = 'AirPlay cleanup timed out.'
        if self.audio_transport: self.audio_transport.close()
        if self.client: self.client.close()
        if self.connection: self.connection.close()
        if self.timing_server and self.timing_callback:
            self.timing_server.datagram_received = self.timing_callback
            self.timing_server = self.timing_callback = None
        self.phase = 'closed'
