from test_paths import TEST_TMP
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from core import Engine, UserError
from test_core import FakeBackend
import setup_runtime


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root, FakeBackend())
        await self.engine.start()

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    async def test_settings_persist_without_announcement_and_survive_restart(self):
        result = self.engine.update_settings({'target': 'lg', 'volume': 19})
        self.assertEqual(result, {'target': 'lg', 'volume': 19})
        self.assertEqual(self.engine.backend.plays, [])
        await self.engine.close()
        self.engine = Engine(self.root, FakeBackend())
        await self.engine.start()
        self.assertEqual(self.engine.settings, result)
        self.assertEqual(len(self.engine.history), 0)

    async def test_settings_reject_invalid_values_without_losing_current_selection(self):
        self.engine.update_settings({'target': 'lg'})
        for data in [{'target': ''}, {'volume': True}, {'volume': 41}, {'volume': float('nan')}, {'url': 'http://evil'}, {}, []]:
            with self.subTest(data=str(data)), self.assertRaises(UserError):
                self.engine.update_settings(data)
        self.assertEqual(self.engine.settings, {'target': 'lg', 'volume': 15})

    async def test_settings_disk_failure_rolls_back_and_does_not_play(self):
        before = self.engine.settings.copy()
        with patch('core.atomic_json', side_effect=OSError('Datenträger voll')):
            with self.assertRaises(UserError):
                self.engine.update_settings({'volume': 19})
        self.assertEqual(self.engine.settings, before)
        self.assertEqual(self.engine.backend.plays, [])

    async def test_settings_are_rejected_during_shutdown(self):
        await self.engine.close()
        with self.assertRaises(UserError):
            self.engine.update_settings({'volume': 12})

    async def test_a_lower_volume_limit_also_limits_the_initial_default(self):
        other = Engine(self.root / 'quiet', FakeBackend(), max_volume=5)
        await other.start()
        self.assertEqual(other.settings['volume'], 5)
        await other.close()


class SetupTests(unittest.TestCase):
    def setUp(self):
        (TEST_TMP).mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name) / 'airplay-mm.99'
        self.root.mkdir()
        (self.root / 'voices').mkdir()
        self.data = b'verified test voice'
        self.expected = hashlib.sha256(self.data).hexdigest()
        self.asset = {'voices/test.onnx': ('https://official.invalid/', self.expected, 100)}

    def tearDown(self):
        self.temp.cleanup()

    def test_verified_existing_asset_does_not_download(self):
        (self.root / 'voices' / 'test.onnx').write_bytes(self.data)
        with patch.dict(setup_runtime.ASSETS, self.asset, clear=True), patch('setup_runtime.urllib.request.urlopen') as request:
            setup_runtime.install_asset(self.root, 'voices/test.onnx')
        request.assert_not_called()

    def test_missing_assets_are_downloaded_without_importing_sibling_data(self):
        sibling = self.root.parent / 'old-runtime'
        (sibling / 'voices').mkdir(parents=True)
        source = sibling / 'voices' / 'test.onnx'
        source.write_bytes(b'outdated sibling model')
        with patch.dict(setup_runtime.ASSETS, self.asset, clear=True), patch(
            'setup_runtime.urllib.request.urlopen', return_value=io.BytesIO(self.data)
        ) as request:
            setup_runtime.install_asset(self.root, 'voices/test.onnx')
        request.assert_called_once()
        self.assertEqual((self.root / 'voices' / 'test.onnx').read_bytes(), self.data)
        self.assertEqual(source.read_bytes(), b'outdated sibling model')

    def test_mismatched_download_is_rejected_and_temporary_file_removed(self):
        with patch.dict(setup_runtime.ASSETS, self.asset, clear=True), patch('setup_runtime.urllib.request.urlopen', return_value=io.BytesIO(b'corrupt')):
            with self.assertRaisesRegex(ValueError, 'Checksum'):
                setup_runtime.install_asset(self.root, 'voices/test.onnx')
        self.assertEqual(list((self.root / 'voices').iterdir()), [])

    def test_oversized_download_is_rejected(self):
        with patch.dict(setup_runtime.ASSETS, self.asset, clear=True), patch('setup_runtime.urllib.request.urlopen', return_value=io.BytesIO(b'x' * 101)):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                setup_runtime.install_asset(self.root, 'voices/test.onnx')

    def test_root_requires_an_adapter_instance_directory(self):
        with self.assertRaises(ValueError):
            setup_runtime.checked_root(str(self.root.parent))
        self.assertEqual(setup_runtime.checked_root(str(self.root)), self.root)

    def test_all_downloads_are_pinned_with_hash_and_bounded(self):
        for relative, (base, sha, limit) in setup_runtime.ASSETS.items():
            self.assertRegex(sha, r'^[a-f0-9]{64}$')
            self.assertTrue(base.startswith('https://'))
            self.assertLessEqual(limit, 70_000_000)
            self.assertNotIn('..', Path(relative).parts)
