"""Own-directory installation. No root, apt, global pip or system services."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from voices import DEFAULT_VOICE, voice_spec

PACKAGES = {'pyatv': '0.18.0', 'piper-tts': '1.8.0', 'aiohttp': '3.14.5', 'miniaudio': '1.71', 'srptools': '1.0.1'}
VOICE_BASE = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/'
ASSETS = {
    'voices/de_DE-thorsten-medium.onnx': (VOICE_BASE, '7e64762d8e5118bb578f2eea6207e1a35a8e0c30595010b666f983fc87bb7819', 70_000_000),
    'voices/de_DE-thorsten-medium.onnx.json': (VOICE_BASE, '974adee790533adb273a1ac88f49027d2a1b8f0f2cf4905954a4791e79264e85', 100_000),
    'voices/MODEL_CARD': (VOICE_BASE, '5196b5ab0794e6056263a1f37c18bec407b61ac187529bee29d1c366871e5c9e', 100_000),
}
RAMONA_BASE = 'https://huggingface.co/rhasspy/piper-voices/resolve/5041a7a49ae5b3a0f30e6837769eb03fdbb231bd/de/de_DE/ramona/low/'
RAMONA_ASSETS = {
    'voices/ramona/de_DE-ramona-low.onnx': (RAMONA_BASE, 'a1068592532aad8c49bf3d1a01b8749c11d7dba726689ee28ad64e7b627ae4c5', 70_000_000),
    'voices/ramona/de_DE-ramona-low.onnx.json': (RAMONA_BASE, '64906ab7a050ac685fa3dcea044baa798f67ed77be164f9c668a69298c6487d2', 100_000),
    'voices/ramona/MODEL_CARD': (RAMONA_BASE, '3b6dbf2cde464e35cbdd3c06fccb34bf8cd590878c251391e9fd0a6f1bb993da', 100_000),
}

def selected_assets(voice=DEFAULT_VOICE):
    voice_spec(voice)
    if voice == DEFAULT_VOICE:
        return dict(ASSETS)
    return {**{key: value for key, value in ASSETS.items() if not key.startswith('voices/')}, **RAMONA_ASSETS}


def emit(message):
    print(json.dumps({'event': 'setup', 'message': message}, ensure_ascii=True), flush=True)


def digest(path: Path):
    if path.is_symlink() or not path.is_file():
        return None
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def install_asset(root: Path, relative: str, legacy: Path | None = None, assets=None):
    base, expected, limit = (ASSETS if assets is None else assets)[relative]
    destination = root / relative
    if destination.parent.is_symlink():
        raise ValueError('Unexpected link at voice directory')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError('Unexpected link at runtime file')
    if digest(destination) == expected:
        return
    temporary = destination.with_name(destination.name + '.download')
    if temporary.is_symlink():
        raise ValueError('Unexpected download link')
    try:
        source = legacy / relative if legacy else None
        if source and source.is_file() and not source.is_symlink() and source.stat().st_size <= limit and digest(source) == expected:
            shutil.copyfile(source, temporary)
        else:
            emit('Downloading local voice file' if relative.startswith('voices/') else 'Downloading AirPlay sender')
            count = 0
            with urllib.request.urlopen(base + destination.name, timeout=45) as response, temporary.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > limit:
                        raise ValueError('Download exceeds size limit')
                    output.write(chunk)
        if digest(temporary) != expected:
            raise ValueError('Checksum does not match: ' + destination.name)
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def checked_root(value: str):
    root = Path(value).absolute()
    if root.resolve() != root or root.is_symlink() or not root.name.startswith('airplay-mm.') or not root.name.split('.')[-1].isdigit():
        raise ValueError('Unexpected adapter data directory')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ['bin', 'voices', 'cache', 'logs']:
        folder = root / name
        if folder.is_symlink():
            raise ValueError('Unexpected directory link')
        folder.mkdir(mode=0o700, exist_ok=True)
    return root


def environment_ok(python: Path, root: Path, log):
    if not python.is_file():
        return False
    code = 'import sys,json;print(json.dumps({"prefix":sys.prefix,"version":sys.version_info[:2]}))'
    result = subprocess.run([str(python), '-c', code], capture_output=True, text=True, timeout=15)
    try:
        data = json.loads(result.stdout)
        return result.returncode == 0 and Path(data['prefix']).resolve() == (root / 'venv').resolve() and data['version'] == list(sys.version_info[:2])
    except (ValueError, KeyError):
        return False


def ensure_venv(root: Path, log):
    venv = root / 'venv'
    if venv.is_symlink():
        raise ValueError('The Python environment must not be a directory link')
    python = venv / 'bin' / 'python'
    if not environment_ok(python, root, log):
        if venv.exists():
            # Preserve an incompatible restored environment for diagnosis. Never
            # recursively delete a computed directory on the user's host.
            venv.rename(root / ('venv.previous-' + str(time.time_ns())))
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True, timeout=90, stdout=log, stderr=log)
    check = ('import importlib.metadata as m,json;'
             'print(json.dumps({p:m.version(p) for p in ' + repr(list(PACKAGES)) + '}))')
    result = subprocess.run([str(python), '-c', check], capture_output=True, text=True, timeout=20)
    if result.returncode == 0 and json.loads(result.stdout) == PACKAGES:
        return python
    emit('Richte isolierte Python-Pakete ein · dies kann einige Minuten dauern')
    if subprocess.run([str(python), '-m', 'pip', '--version'], stdout=log, stderr=log, timeout=15).returncode:
        with urllib.request.urlopen('https://pypi.org/pypi/pip/json', timeout=30) as response:
            metadata = json.load(response)
        wheel = next(row for row in metadata['urls'] if row['filename'].endswith('py3-none-any.whl'))
        with urllib.request.urlopen(wheel['url'], timeout=45) as response:
            data = response.read(10_000_001)
        if len(data) > 10_000_000 or hashlib.sha256(data).hexdigest() != wheel['digests']['sha256']:
            raise ValueError('pip package checksum does not match')
        file = root / Path(wheel['filename']).name
        file.write_bytes(data)
        bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('pip',run_name='__main__')"
        subprocess.run([str(python), '-c', bootstrap, str(file), 'install', '--no-index', '--no-input', str(file)], check=True, timeout=60, stdout=log, stderr=log)
        file.unlink(missing_ok=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--only-binary=:all:',
                    *[name + '==' + version for name, version in PACKAGES.items()]], check=True, timeout=360, stdout=log, stderr=log)
    result = subprocess.run([str(python), '-c', check], capture_output=True, text=True, timeout=20, check=True)
    if json.loads(result.stdout) != PACKAGES:
        raise ValueError('Installed Python package versions do not match')
    return python


def main(args):
    if sys.version_info < (3, 11) or sys.version_info >= (3, 14) or sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise ValueError('Requires Linux x64 with Python 3.11 to 3.13')
    root = checked_root(args.root)
    legacy = None
    if args.legacy:
        legacy = Path(args.legacy).absolute()
        if legacy != root.parent / 'airplay-mm' or legacy.resolve() != legacy or legacy.is_symlink():
            raise ValueError('Unexpected legacy source path')
    log_path = root / 'logs' / 'installation.log'
    if log_path.exists() and log_path.stat().st_size > 500_000:
        log_path.replace(log_path.with_suffix('.previous.log'))
    with log_path.open('a', encoding='utf-8') as log:
        python = ensure_venv(root, log)
        assets = selected_assets(args.voice)
        emit('Checking local voice: ' + voice_spec(args.voice)['name'])
        for relative in assets:
            install_asset(root, relative, legacy, assets)
        subprocess.run([str(python), '-c', 'import pyatv,piper,miniaudio,aiohttp'], check=True, timeout=30, stdout=log, stderr=log)
    (root / 'runtime-installed.json').write_text(json.dumps({'schema': 1, 'packages': PACKAGES, 'verifiedAt': time.time()}, indent=2), encoding='utf-8')
    print(json.dumps({'installed': True, 'root': str(root), 'python': str(python), 'pythonVersion': platform.python_version()}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--legacy')
    parser.add_argument('--voice', default=DEFAULT_VOICE)
    try:
        main(parser.parse_args())
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__ + ': ' + str(exc)[:2000], 'log': 'logs/installation.log'}, ensure_ascii=True), flush=True)
        sys.exit(1)
