"""Private credential storage; original sender keys remain a separate backup."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import secrets
import time

from core import UserError, atomic_json

LOG = logging.getLogger('airplay-mm')


def redact(value, *, limit=700):
    text = str(value)
    text = re.sub(r'(?i)(?:[0-9a-f]{2})+(?::(?:[0-9a-f]{2})+){3}', '[pairing key]', text)
    text = re.sub(r'(?i)(?:CREDENTIALS:\s*)?[0-9a-f]{192}', '[pairing key]', text)
    text = re.sub(r'\b\d{3}-\d{2}-\d{3}\b', '[PIN]', text)
    return text[:limit]


class Credentials:
    def __init__(self, root: Path):
        self.path = root / 'pairing-credentials.json'
        self.records = {}
        self.pyatv_path = root / 'pairing-pyatv.json'
        self.pyatv_records = {}
        self.error = ''
        if self.pyatv_path.exists() or self.pyatv_path.is_symlink():
            try:
                if self.pyatv_path.is_symlink() or not self.pyatv_path.is_file() or self.pyatv_path.stat().st_size>200_000: raise ValueError('Invalid pairing path')
                data=json.loads(self.pyatv_path.read_text(encoding='utf-8'))
                if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('devices'),dict) or len(data['devices'])>32: raise ValueError('Invalid pairing data')
                for key,row in data['devices'].items():
                    if not isinstance(key,str) or not 1<=len(key)<=128 or not isinstance(row,dict) or not self.valid_pyatv(row.get('pyatv')): raise ValueError('Invalid pairing key')
                self.pyatv_records=data['devices']
            except (OSError,ValueError,TypeError):
                self.error='Pairing file could not be read safely; existing files are preserved.'
                LOG.warning(self.error)
        if not self.path.exists() and not self.path.is_symlink():
            return
        try:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 200_000:
                raise ValueError('Invalid pairing file')
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('devices'), dict) or len(value['devices']) > 32:
                raise ValueError('Invalid pairing format')
            for key, row in value['devices'].items():
                if not isinstance(key, str) or not 1 <= len(key) <= 128 or not self.valid(row):
                    raise ValueError('Invalid pairing entry')
            self.records = value['devices']
            self.path.chmod(0o600)
        except (OSError, ValueError, TypeError):
            self.error = 'Pairing file could not be read safely; the existing file is preserved.'
            LOG.warning(self.error)

    @staticmethod
    def valid(row):
        return (isinstance(row, dict) and isinstance(row.get('credentials'), str)
                and re.fullmatch(r'[0-9a-fA-F]{192}', row['credentials']) is not None
                and isinstance(row.get('dacp'), str)
                and re.fullmatch(r'[0-9A-F]{16}', row['dacp']) is not None)

    def get(self, target):
        return self.pyatv_records.get(target) or self.records.get(target)

    @staticmethod
    def valid_pyatv(value):
        if not isinstance(value,str) or len(value)>2048 or not re.fullmatch(r'[0-9a-fA-F]+(?::[0-9a-fA-F]+){3}',value): return False
        try:
            from pyatv.auth.hap_pairing import parse_credentials,AuthenticationType
            c=parse_credentials(value)
            return c.type==AuthenticationType.HAP and len(c.ltpk)==len(c.ltsk)==32 and 1<=len(c.atv_id)<=128 and 1<=len(c.client_id)<=128
        except Exception: return False

    def save_pyatv(self,target,value):
        if self.error or not isinstance(target,str) or not 1<=len(target)<=128 or not self.valid_pyatv(value): raise UserError('Invalid pairing; existing keys are preserved.')
        if self.pyatv_path.is_symlink() or self.pyatv_path.with_name(self.pyatv_path.name+'.tmp').is_symlink(): raise UserError('Unsafe pairing file.')
        if target not in self.pyatv_records and len(self.pyatv_records)>=32: raise UserError('Up to 32 saved pairings are supported.')
        trial={**self.pyatv_records,target:{'pyatv':value,'updated':time.time()}}
        atomic_json(self.pyatv_path,{'schema':1,'devices':trial})
        self.pyatv_records=trial

    def save(self, target, row):
        if self.error or self.path.is_symlink() or not self.valid(row):
            raise UserError('Pairing key could not be saved safely.')
        if target not in self.records and len(self.records) >= 32:
            raise UserError('Up to 32 saved pairings are supported.')
        trial = {**self.records, target: row}
        temporary = self.path.with_name(self.path.name + '.tmp')
        if temporary.is_symlink():
            raise UserError('Unsafe temporary pairing file.')
        atomic_json(self.path, {'schema': 1, 'devices': trial})
        self.records = trial

    def forget(self, target):
        if target in self.pyatv_records:
            if self.error or self.pyatv_path.is_symlink() or self.pyatv_path.with_name(self.pyatv_path.name+'.tmp').is_symlink(): raise UserError('Unsafe pairing file.')
            trial={k:v for k,v in self.pyatv_records.items() if k!=target}
            atomic_json(self.pyatv_path,{'schema':1,'devices':trial})
            self.pyatv_records=trial
        if self.error or self.path.is_symlink():
            raise UserError('Pairing file cannot be changed safely.')
        trial = {k: v for k, v in self.records.items() if k != target}
        if self.path.with_name(self.path.name + '.tmp').is_symlink():
            raise UserError('Unsafe temporary pairing file.')
        atomic_json(self.path, {'schema': 1, 'devices': trial})
        self.records = trial


from pyatv_pairing import PyatvPairing as Pairing
