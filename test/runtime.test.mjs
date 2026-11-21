import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import net from 'node:net';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';

const require = createRequire(import.meta.url);
const { checkPort, run } = require('../lib/runtime');
const runtimeUrl = new URL('../lib/runtime.js', import.meta.url);
const runtimeSource = await fs.readFile(runtimeUrl, 'utf8');
const runtimeRequire = createRequire(runtimeUrl);
const packageRoot = fileURLToPath(new URL('../', import.meta.url));
const tempBase = fileURLToPath(new URL('./.tmp/', import.meta.url));

function isolatedRuntime(platform = 'linux', arch = 'x64') {
    const module = { exports: {} };
    vm.runInNewContext(
        runtimeSource,
        {
            module,
            require: runtimeRequire,
            process: { platform, arch, env: {}, kill: () => assert.fail('No real process signals in tests') },
            setTimeout,
            clearTimeout,
        },
        { filename: fileURLToPath(runtimeUrl) },
    );
    return module.exports;
}

async function fixture(t) {
    await fs.mkdir(tempBase, { recursive: true });
    const base = await fs.mkdtemp(path.join(tempBase, 'runtime-'));
    assert.ok(base.startsWith(tempBase));
    t.after(async () => {
        assert.ok((await fs.realpath(base)).startsWith((await fs.realpath(tempBase)) + path.sep));
        await fs.rm(base, { recursive: true, force: true });
    });
    const root = path.join(base, 'airplay-mm.0');
    await fs.mkdir(root);
    return { base, root };
}

const config = {
    host: '127.0.0.1',
    port: 0,
    network: '127.0.0.0/8',
    speechProvider: 'piper',
    piperVoice: 'de_DE-thorsten-medium',
};

function installerResult(root) {
    return { installed: true, root, python: path.join(root, 'venv', 'bin', 'python'), pythonVersion: '3.12' };
}

test('setup preserves the current instance speech cache, settings and history', async (t) => {
    const { base, root } = await fixture(t);
    await fs.mkdir(path.join(root, 'cache'));
    const saved = {
        'cache/saved.wav': Buffer.from('existing cached speech'),
        'settings.json': Buffer.from('{"target":"saved","volume":55,"masterGain":25}'),
        'history.json': Buffer.from('[{"id":"completed","status":"completed"}]'),
    };
    for (const [name, data] of Object.entries(saved)) await fs.writeFile(path.join(root, name), data);
    const sibling = path.join(base, 'old-runtime');
    await fs.mkdir(sibling);
    await fs.writeFile(path.join(sibling, 'config.json'), 'unreadable old configuration');
    let invoked = 0;
    await isolatedRuntime().prepare(root, packageRoot, config, {
        spawnFn: (command, args) => {
            invoked++;
            assert.equal(command, 'python3');
            assert.ok(args.includes(path.join(packageRoot, 'worker', 'setup_runtime.py')));
            assert.equal(args[args.indexOf('--root') + 1], root);
            assert.ok(!args.includes('--legacy'));
            return processStub(JSON.stringify(installerResult(root)) + '\n');
        },
    });
    assert.equal(invoked, 1);
    for (const [name, data] of Object.entries(saved))
        assert.deepEqual(await fs.readFile(path.join(root, name)), data);
    assert.equal(
        await fs.readFile(path.join(sibling, 'config.json'), 'utf8'),
        'unreadable old configuration',
    );
    assert.deepEqual(JSON.parse(await fs.readFile(path.join(root, 'config.json'), 'utf8')), config);
});

test('provider selection keeps Ramona fallback and does not force unrelated voice downloads', async (t) => {
    const { root } = await fixture(t);
    for (const [speechProvider, piperVoice, expected] of [
        ['speechify', 'de_DE-thorsten-medium', 'de_DE-ramona-low'],
        ['sayit-google', 'de_DE-ramona-low', 'de_DE-thorsten-medium'],
        ['piper', 'de_DE-ramona-low', 'de_DE-ramona-low'],
    ]) {
        await isolatedRuntime().prepare(
            root,
            packageRoot,
            { ...config, speechProvider, piperVoice },
            {
                spawnFn: (_command, args) => {
                    assert.equal(args[args.indexOf('--voice') + 1], expected);
                    return processStub(JSON.stringify(installerResult(root)) + '\n');
                },
            },
        );
    }
});

test('a setup result for a different instance is rejected before configuration is saved', async (t) => {
    const { root } = await fixture(t);
    await assert.rejects(
        isolatedRuntime().prepare(root, packageRoot, config, {
            spawnFn: () =>
                processStub(JSON.stringify({ ...installerResult(root), root: root + '-other' }) + '\n'),
        }),
        /Runtime verification failed/,
    );
    await assert.rejects(fs.stat(path.join(root, 'config.json')), { code: 'ENOENT' });
});

test('unsupported platforms fail without creating data or starting Python', async (t) => {
    const { base } = await fixture(t);
    const root = path.join(base, 'unsupported.0');
    for (const [platform, arch] of [
        ['win32', 'x64'],
        ['linux', 'arm64'],
    ]) {
        await assert.rejects(
            isolatedRuntime(platform, arch).prepare(root, packageRoot, config, {
                spawnFn: () => assert.fail('Unsupported platforms must not start Python'),
            }),
            /requires Linux/,
        );
    }
    await assert.rejects(fs.stat(root), { code: 'ENOENT' });
});

test('occupied ports produce a precise instruction and do not stop the existing listener', async () => {
    const server = net.createServer();
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
        const { port } = server.address();
        await assert.rejects(checkPort('127.0.0.1', port), /Port .*already in use/);
        assert.equal(server.listening, true);
    } finally {
        await new Promise((resolve) => server.close(resolve));
    }
});
function processStub(output, code = 0) {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.pid = undefined;
    queueMicrotask(() => {
        child.stdout.write(output);
        child.emit('close', code);
    });
    return child;
}
test('setup stdout is bounded and only the final JSON result is interpreted', async () => {
    const progress = [];
    const result = await run('fake', [], {
        spawnFn: () => processStub('{"event":"setup","message":"Fortschritt"}\n{"installed":true}\n'),
        onLine: (x) => progress.push(x),
    });
    assert.equal(result.installed, true);
    assert.equal(progress[0].message, 'Fortschritt');
});
test('setup spawn failures and malformed results reject rather than hanging', async () => {
    await assert.rejects(
        run('fake', [], {
            spawnFn: () => {
                throw new Error('missing');
            },
        }),
        /missing/,
    );
    await assert.rejects(run('fake', [], { spawnFn: () => processStub('kein JSON\n') }), /valid response/);
    await assert.rejects(
        run('fake', [], { spawnFn: () => processStub('{"error":"offline"}\n', 1) }),
        /offline/,
    );
});
test('an aborted setup cannot start another process', async () => {
    const abort = new AbortController();
    abort.abort();
    let spawned = false;
    await assert.rejects(
        run('fake', [], {
            signal: abort.signal,
            spawnFn: () => {
                spawned = true;
            },
        }),
        /cancelled/,
    );
    assert.equal(spawned, false);
});
