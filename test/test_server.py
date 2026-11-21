from test_paths import TEST_TMP
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from core import Engine
from server import create_app
from test_core import FakeBackend


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP, prefix='server-')
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to((TEST_TMP).resolve())
        self.backend = FakeBackend()
        self.engine = Engine(self.root, self.backend)
        await self.engine.start()
        await self.engine.discover()
        self.hosts = set()
        self.client = TestClient(TestServer(create_app(self.engine, BASE / 'web', self.hosts)))
        await self.client.start_server()
        self.hosts.add(self.client.make_url('/').raw_authority)
        response = await self.client.get('/api/status')
        self.csrf = (await response.json())['csrf']

    async def asyncTearDown(self):
        await self.client.close()
        await self.engine.close()
        assert self.root.is_relative_to((TEST_TMP).resolve())
        self.temp.cleanup()

    def request(self, **changes):
        return {'text': 'Hallo Welt', 'target': 'lg', 'volume': 15, 'requestId': 'http-test', **changes}

    async def post(self, path, payload):
        return await self.client.post('/api/' + path, json=payload, headers={'X-AirPlay-CSRF': self.csrf})

    async def test_static_page_and_security_headers(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        self.assertIn('AirPlay MM', await response.text())
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        icons = await self.client.get('/device-icons.js')
        self.assertEqual(icons.status, 200)
        self.assertIn('AirPlayDeviceIcons', await icons.text())

    async def test_submit_and_result(self):
        response = await self.post('speak', self.request())
        self.assertEqual(response.status, 202)
        await asyncio.wait_for(self.engine.queue.join(), 1)
        state = await (await self.client.get('/api/status')).json()
        self.assertEqual(state['history'][0]['status'], 'completed')

    async def test_missing_csrf_cannot_play(self):
        response = await self.client.post('/api/speak', json=self.request())
        self.assertEqual(response.status, 403)
        self.assertFalse(self.backend.plays)

    async def test_master_setting_is_independent_of_receiver_volume_and_does_not_play(self):
        response=await self.post('settings',{'masterGain':50})
        self.assertEqual(response.status,200)
        state=await (await self.client.get('/api/status')).json()
        self.assertEqual(state['masterGain'],50);self.assertEqual(state['settings']['volume'],15)
        self.assertEqual(self.backend.plays,[])

    async def test_cross_origin_cannot_play(self):
        response = await self.client.post('/api/speak', json=self.request(), headers={'X-AirPlay-CSRF': self.csrf, 'Origin': 'https://fremd.invalid'})
        self.assertEqual(response.status, 403)
        self.assertFalse(self.backend.plays)

    async def test_dns_rebinding_host_is_rejected(self):
        response = await self.client.get('/api/status', headers={'Host': 'fremd.invalid'})
        self.assertEqual(response.status, 403)

    async def test_oversized_body_cannot_play(self):
        response = await self.post('speak', self.request(text='x' * 10_000))
        self.assertEqual(response.status, 413)
        self.assertFalse(self.backend.plays)

    async def test_invalid_json_is_handled(self):
        response = await self.client.post('/api/speak', data='{', headers={'X-AirPlay-CSRF': self.csrf, 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)
        self.assertFalse(self.backend.plays)

    async def test_unknown_fields_and_external_url_rejected(self):
        response = await self.post('speak', {**self.request(), 'url': 'http://example.invalid'})
        self.assertEqual(response.status, 400)
        self.assertFalse(self.backend.plays)

    async def test_discovery_and_cancel(self):
        response = await self.post('discover', {})
        self.assertEqual(response.status, 200)
        self.backend.release = asyncio.Event()
        await self.post('speak', self.request())
        await self.backend.entered.wait()
        response = await self.post('cancel', {})
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(self.engine.queue.join(), 1)
        self.assertEqual(self.engine.history[-1]['status'], 'cancelled')

    async def test_static_path_cannot_escape(self):
        response = await self.client.get('/config.json')
        self.assertEqual(response.status, 404)

    async def test_settings_endpoint_persists_selection_without_playback(self):
        response = await self.post('settings', {'target': 'lg', 'volume': 18})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.engine.settings, {'target': 'lg', 'volume': 18})
        self.assertFalse(self.backend.plays)

    async def test_settings_endpoint_rejects_unprotected_or_invalid_writes(self):
        response = await self.client.post('/api/settings', json={'volume': 19})
        self.assertEqual(response.status, 403)
        response = await self.post('settings', {'url': 'http://foreign.invalid'})
        self.assertEqual(response.status, 400)
        self.assertFalse(self.backend.plays)

    async def test_prepare_endpoint_generates_without_history_or_audio(self):
        response = await self.post('precache', {'text': 'Ein still vorbereiteter Text'})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['result']['prepared'])
        self.assertFalse(self.backend.plays)
        self.assertFalse(self.engine.history)
        self.assertFalse(self.engine.snapshot()['preparingText'])

    async def test_prepare_endpoint_requires_csrf_and_valid_text(self):
        response = await self.client.post('/api/precache', json={'text': 'Hallo'})
        self.assertEqual(response.status, 403)
        for payload in [{'text': ''}, {'text': 'x' * 501}, {'text': 'Hallo\x00'}, {'text': 'Hallo', 'url': 'fremd'}, ['Hallo']]:
            response = await self.post('precache', payload)
            self.assertEqual(response.status, 400)
        self.assertFalse(self.backend.plays)


if __name__ == '__main__':
    unittest.main()
