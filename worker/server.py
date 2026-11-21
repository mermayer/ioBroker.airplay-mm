"""Local UI/API and JSON-lines bridge to the ioBroker script."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import signal
import sys
from urllib.parse import urlsplit

from aiohttp import web

from core import Engine, UserError, VERSION

LOG = logging.getLogger('airplay-mm')


def create_app(engine: Engine, web_root: Path, allowed_hosts: set[str]):
    csrf = secrets.token_urlsafe(32)

    @web.middleware
    async def security(request, handler):
        host = request.headers.get('Host', '').lower()
        if host not in allowed_hosts:
            raise web.HTTPForbidden(text='Host not allowed')
        origin = request.headers.get('Origin')
        if origin and origin != 'http://' + host:
            raise web.HTTPForbidden(text='Origin not allowed')
        if request.method == 'POST' and not secrets.compare_digest(request.headers.get('X-AirPlay-CSRF', ''), csrf):
            raise web.HTTPForbidden(text='Start this request through the web interface')
        try:
            response = await handler(request)
        except UserError as exc:
            response = web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = web.json_response({'ok': False, 'error': 'Invalid request'}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:
            LOG.exception('Web interface request failed')
            response = web.json_response({'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:600]}, status=500)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'",
        })
        return response

    app = web.Application(middlewares=[security], client_max_size=8192)

    async def status(request):
        return web.json_response({'ok': True, 'csrf': csrf, **engine.snapshot()})

    async def speak(request):
        return web.json_response({'ok': True, 'job': await engine.submit(await request.json())}, status=202)

    async def discover(request):
        return web.json_response({'ok': True, 'devices': await engine.discover()})

    async def cancel(request):
        return web.json_response({'ok': True, 'cancelled': await engine.cancel()})

    async def settings(request):
        return web.json_response({'ok': True, 'settings': engine.update_settings(await request.json())})

    async def precache(request):
        return web.json_response({'ok': True, 'result': await engine.precache(await request.json())})

    async def cache(request):
        data = {} if request.method == 'GET' else await request.json()
        if request.match_info['action'] == 'cachePlay':
            result = await engine.play_cached(data)
        else:
            result = await engine.cache_command(request.match_info['action'], data)
        return web.json_response({'ok': True, 'result': result})

    async def pairing(request):
        return web.json_response({'ok': True, 'result': await engine.pairing_command(request.match_info['action'], await request.json())})

    async def static(request):
        files = {'/': 'index.html', '/app.js': 'app.js', '/device-icons.js': 'device-icons.js', '/app.css': 'app.css', '/icon.svg': 'icon.svg'}
        filename = files.get(request.path)
        if not filename:
            raise web.HTTPNotFound()
        return web.FileResponse(web_root / filename)

    app.router.add_get('/api/status', status)
    app.router.add_post('/api/speak', speak)
    app.router.add_post('/api/discover', discover)
    app.router.add_post('/api/cancel', cancel)
    app.router.add_post('/api/settings', settings)
    app.router.add_post('/api/precache', precache)
    app.router.add_get('/api/{action:cacheList}', cache)
    app.router.add_post('/api/{action:cacheList|cachePlay|cacheDelete|retryLearning}', cache)
    app.router.add_post('/api/{action:pairStart|pairFinish|pairCancel|forgetPairing|disconnect}', pairing)
    for path in ['/', '/app.js', '/device-icons.js', '/app.css', '/icon.svg']:
        app.router.add_get(path, static)
    return app


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


class JsonLogHandler(logging.Handler):
    def emit(self, record):
        from pairing import redact
        level = {'WARNING': 'warn', 'CRITICAL': 'error'}.get(record.levelname, record.levelname.lower())
        message = redact(record.getMessage(), limit=1900)
        emit({'event': 'log', 'level': level, 'message': message})
        if record.exc_info:
            trace = redact(logging.Formatter().formatException(record.exc_info), limit=9000)
            for offset in range(0, len(trace), 1800):
                emit({'event': 'log', 'level': level, 'message': 'Error details: ' + trace[offset:offset + 1800]})


async def read_commands(engine: Engine, stopped: asyncio.Event):
    reader = asyncio.StreamReader(limit=16384)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    preparations = set()
    async def prepare(message):
        try:
            result = await engine.precache(message.get('data'))
            emit({'id': message.get('id'), 'ok': True, 'result': result})
        except Exception as error:
            emit({'id': message.get('id'), 'ok': False, 'error': type(error).__name__ + ': ' + str(error)[:600]})
    try:
        while not stopped.is_set():
            line = await reader.readline()
            if not line:
                stopped.set()
                return
            message = {}
            try:
                message = json.loads(line)
                action = message.get('action')
                if action == 'speak':
                    result = await engine.submit(message.get('data'))
                elif action == 'discover':
                    result = await engine.discover()
                elif action == 'cancel':
                    result = await engine.cancel()
                elif action == 'status':
                    result = engine.snapshot()
                elif action == 'settings':
                    result = engine.update_settings(message.get('data'))
                elif action == 'cachePlay':
                    result = await engine.play_cached(message.get('data'))
                elif action in {'cacheList', 'cacheDelete', 'learn', 'retryLearning', 'setSpeechifyKey'}:
                    result = await engine.cache_command(action, message.get('data'))
                elif action == 'precache':
                    if preparations:
                        raise UserError('A text is already being prepared')
                    task = asyncio.create_task(prepare(message))
                    preparations.add(task)
                    task.add_done_callback(preparations.discard)
                    continue
                elif action in {'pairStart', 'pairFinish', 'pairCancel', 'forgetPairing', 'disconnect'}:
                    result = await engine.pairing_command(action, message.get('data'))
                else:
                    raise UserError('Unknown request')
                emit({'id': message.get('id'), 'ok': True, 'result': result})
            except Exception as exc:
                emit({'id': message.get('id'), 'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:600]})
    finally:
        stopped.set()
        transport.close()
        for task in preparations:
            task.cancel()
        await asyncio.gather(*preparations, return_exceptions=True)


async def main(args):
    from airplay import AirPlayBackend
    root = Path(args.root).resolve()
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    host, port = config['host'], int(config['port'])
    if not 1024 <= port <= 65535:
        raise ValueError('Invalid port')
    handler = RotatingFileHandler(root / 'logs' / 'airplay-mm.log', maxBytes=256_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    LOG.setLevel(logging.INFO)
    LOG.addHandler(JsonLogHandler(level=logging.INFO))
    stopped = asyncio.Event()
    engine = Engine(root, AirPlayBackend(root, config), lambda state: emit({'event': 'status', 'state': state}),
                    max_volume=int(config.get('maxVolume', 40)))
    await engine.start()
    app = create_app(engine, Path(__file__).resolve().parent.parent / 'web', {host.lower() + ':' + str(port), '127.0.0.1:' + str(port), 'localhost:' + str(port)})
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    emit({'event': 'ready', 'url': f'http://{host}:{port}/', 'version': VERSION})
    LOG.info('AirPlay MM started: %s:%s', host, port)
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)

    async def initial_search():
        try:
            await engine.discover()
        except Exception:
            LOG.exception('Initial receiver discovery failed')

    async def heartbeat():
        while not stopped.is_set():
            await asyncio.sleep(3)
            emit({'event': 'status', 'state': engine.snapshot()})

    tasks = [asyncio.create_task(initial_search()), asyncio.create_task(heartbeat())]
    if not args.standalone:
        tasks.append(asyncio.create_task(read_commands(engine, stopped)))
    try:
        await stopped.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.close()
        await runner.cleanup()
        LOG.info('AirPlay MM stopped cleanly')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).parent))
    parser.add_argument('--standalone', action='store_true')
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as exc:
        emit({'event': 'fatal', 'error': type(exc).__name__ + ': ' + str(exc)[:1000]})
        sys.exit(1)
