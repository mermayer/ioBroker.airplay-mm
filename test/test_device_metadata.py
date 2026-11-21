from test_paths import TEST_TMP
import asyncio
import ipaddress
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'worker'))
from device_metadata import receiver_metadata
from airplay import AirPlayBackend
from pyatv.const import PairingRequirement


class MetadataTests(unittest.TestCase):
    def test_homepod_mini_new_generation_keeps_raw_family_identifier(self):
        receiver = SimpleNamespace(device_info=SimpleNamespace(model_str='AudioAccessory11,1', raw_model='AudioAccessory11,1'))
        self.assertEqual(receiver_metadata(receiver), {'model':'AudioAccessory11,1','modelIdentifier':'AudioAccessory11,1'})

    def test_model_from_airplay_and_raop_properties_if_device_info_missing(self):
        self.assertEqual(receiver_metadata(object(), SimpleNamespace(properties={'model':'Mac14,12'})), {'model':'Mac14,12','modelIdentifier':'Mac14,12'})
        self.assertEqual(receiver_metadata(object(), None, SimpleNamespace(properties={'am':'Sony_XR-65X95L'})), {'model':'Sony_XR-65X95L','modelIdentifier':'Sony_XR-65X95L'})

    def test_missing_unknown_and_bad_metadata_is_harmless(self):
        self.assertEqual(receiver_metadata(object(),None),{})
        self.assertEqual(receiver_metadata(SimpleNamespace(device_info=SimpleNamespace(model_str={},raw_model=False)),SimpleNamespace(properties=None)),{})
        class Broken:
            @property
            def device_info(self):
                raise RuntimeError('invalid optional metadata')
        self.assertEqual(receiver_metadata(Broken()),{})

    def test_metadata_is_bounded_and_unrelated_mdns_properties_are_never_exposed(self):
        row=receiver_metadata(object(),SimpleNamespace(properties={'model':'AudioAccessory11,1\x00','pin':'1234','credentials':'secret'}))
        self.assertEqual(row,{'model':'AudioAccessory11,1','modelIdentifier':'AudioAccessory11,1'})
        row=receiver_metadata(object(),SimpleNamespace(properties={'model':'x'*10000}))
        self.assertEqual(len(row['model']),256)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_discovery_path_adds_only_optional_metadata_and_keeps_ids_and_pairing_rules(self):
        temp_base=TEST_TMP;temp_base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_base,prefix='icons-') as directory:
            root=Path(directory);assert root.resolve().is_relative_to(temp_base.resolve())
            backend=AirPlayBackend(root,{'network':'10.0.0.0/24','host':'10.0.0.210'})
            service=SimpleNamespace(pairing=PairingRequirement.NotNeeded,requires_password=False,properties={'model':'AudioAccessory11,1'})
            receiver=SimpleNamespace(identifier='unchanged-id',name='Example room',address=ipaddress.ip_address('10.0.0.225'),get_service=lambda protocol:service,device_info=SimpleNamespace(model_str='AudioAccessory11,1',raw_model='AudioAccessory11,1'))
            async def scan(*args,**kwargs):return [receiver]
            with patch('airplay.pyatv.scan',scan):rows=await backend.discover()
            self.assertEqual(rows[0]['id'],'unchanged-id');self.assertEqual(rows[0]['name'],'Example room');self.assertEqual(rows[0]['modelIdentifier'],'AudioAccessory11,1');self.assertTrue(rows[0]['available']);self.assertFalse(rows[0]['paired']);self.assertIs(backend.configs['unchanged-id'],receiver)
            self.assertFalse((root/'settings.json').exists());self.assertFalse((root/'pairing-credentials.json').exists())

if __name__=='__main__':unittest.main()
