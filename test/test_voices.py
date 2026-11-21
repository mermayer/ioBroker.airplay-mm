from test_paths import TEST_TMP
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from voices import DEFAULT_VOICE, voice_spec
from setup_runtime import RAMONA_ASSETS, selected_assets, install_asset

class VoiceTests(unittest.TestCase):
    def test_allowlist_rejects_external_models_and_paths(self):
        self.assertEqual(voice_spec()['id'], DEFAULT_VOICE)
        for value in ['', None, '../../x', 'https://example.org/model', {}]:
            with self.assertRaises(ValueError):
                voice_spec(value)

    def test_ramona_assets_are_pinned_and_do_not_force_a_thorsten_download(self):
        assets = selected_assets('de_DE-ramona-low')
        self.assertIn(voice_spec('de_DE-ramona-low')['model'], assets)
        self.assertNotIn('voices/de_DE-thorsten-medium.onnx', assets)
        self.assertTrue(all(name.startswith('voices/') for name in assets))
        for base, sha, limit in RAMONA_ASSETS.values():
            self.assertIn('5041a7a49ae5b3a0f30e6837769eb03fdbb231bd', base)
            self.assertEqual(len(sha), 64)
            self.assertGreater(limit, 100)
        with self.assertRaises(ValueError):
            selected_assets('invalid')

    def test_nested_download_is_verified_once_and_then_works_offline(self):
        folder = TEST_TMP
        folder.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            root = Path(tmp)
            content = b'verified ramona model'
            name = 'voices/ramona/test.onnx'
            assets = {name: ('https://example.invalid/', hashlib.sha256(content).hexdigest(), 100)}
            with patch('setup_runtime.urllib.request.urlopen', return_value=io.BytesIO(content)) as request:
                install_asset(root, name, assets=assets)
                request.assert_called_once()
            with patch('setup_runtime.urllib.request.urlopen', side_effect=AssertionError('offline')):
                install_asset(root, name, assets=assets)
            self.assertEqual((root / name).read_bytes(), content)
