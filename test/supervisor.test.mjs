import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { childProcess, pause, until } from './helpers.mjs';
const require = createRequire(import.meta.url);
const { Supervisor } = require('../lib/supervisor');
const activeSupervisors = new Set();
test.afterEach(async () => {
    for (const supervisor of activeSupervisors) await supervisor.stop();
    activeSupervisors.clear();
});
function fixture(options = {}) {
    const children = [],
        signals = [],
        faults = [];
    let supervisor;
    const spawn = () => {
        const child = childProcess();
        child.pid += children.length;
        children.push(child);
        return child;
    };
    supervisor = new Supervisor('/data/airplay-mm.0', '/package', {
        spawn,
        signalGroup: (pid, signal) => {
            signals.push({ pid, signal });
            const child = children.find((c) => c.pid === pid);
            if (signal === 'SIGTERM' || signal === 'SIGKILL')
                queueMicrotask(() => child.emit('close', 0, signal));
        },
        delay: () => 5,
        watchdogMs: 1000,
        ...options,
    });
    activeSupervisors.add(supervisor);
    supervisor.on('fault', (error) => faults.push(error));
    return { supervisor, children, signals, faults };
}
const ready = (child) =>
    child.stdout.write(Buffer.from('{"event":"ready","url":"http://127.0.0.1:8098/"}\n'));
test('supervisor invokes only the packaged worker with its own data directory', async () => {
    let args;
    const { supervisor, children } = fixture({
        spawn: (command, argv, opts) => {
            args = { command, argv, opts };
            const child = childProcess();
            children.push(child);
            return child;
        },
    });
    supervisor.start();
    assert.equal(args.command, require('node:path').join('/data/airplay-mm.0', 'venv', 'bin', 'python'));
    assert.ok(args.argv.includes('--root'));
    assert.equal(args.opts.detached, true);
    await supervisor.stop();
});
test('no commands enter a starting or stopped worker', async () => {
    const { supervisor } = fixture();
    supervisor.start();
    await assert.rejects(supervisor.request('speak', {}), /not ready/);
    await supervisor.stop();
    await assert.rejects(supervisor.request('speak', {}), /not ready/);
});
test('UTF-8 split across chunks and response IDs are reconstructed correctly', async () => {
    const { supervisor, children } = fixture();
    supervisor.start();
    ready(children[0]);
    const log = Buffer.from('{"event":"log","message":"Grüße"}\n');
    let row;
    supervisor.on('log', (x) => {
        row = x;
    });
    const split = log.indexOf(Buffer.from('ü')) + 1;
    children[0].stdout.write(log.subarray(0, split));
    children[0].stdout.write(log.subarray(split));
    assert.equal(row.message, 'Grüße');
    const pending = supervisor.request('status');
    children[0].stdout.write(Buffer.from('{"id":"1","ok":true,"result":{"ready":true}}\n'));
    assert.equal((await pending).ready, true);
    await supervisor.stop();
});
test('worker failures reject pending commands and restart without replay', async () => {
    const { supervisor, children } = fixture();
    supervisor.start();
    ready(children[0]);
    const pending = supervisor.request('speak', { text: 'Hallo' });
    const rejection = assert.rejects(pending, /stopped/);
    children[0].emit('close', 1, null);
    await rejection;
    await until(() => children.length === 2);
    assert.equal(supervisor.pending.size, 0);
    assert.equal(children[1].stdin.readableLength, 0);
    await supervisor.stop();
});
test('a timed-out command is rejected once and never resent', async () => {
    const { supervisor, children } = fixture({ requestTimeout: 5 });
    supervisor.start();
    ready(children[0]);
    await assert.rejects(supervisor.request('speak', { text: 'Hallo' }), /not.*repeated automatically/);
    assert.equal(supervisor.pending.size, 0);
    const sent = children[0].stdin.read().toString();
    assert.equal(sent.trim().split('\n').length, 1);
    await supervisor.stop();
});
test('stdin write failures are isolated and clear their timer', async () => {
    const { supervisor, children } = fixture();
    supervisor.start();
    ready(children[0]);
    children[0].stdin.write = () => {
        throw new Error('EPIPE');
    };
    await assert.rejects(supervisor.request('status'), /EPIPE/);
    assert.equal(supervisor.pending.size, 0);
    await supervisor.stop();
});
test('restart delay increases across rapid crashes and remains bounded', async () => {
    const delays = [];
    const { supervisor, children } = fixture({
        delay: (retry) => {
            delays.push(Math.min(60, 2 ** retry));
            return 1;
        },
    });
    supervisor.start();
    for (let i = 0; i < 8; i++) {
        await until(() => children.length > i);
        ready(children[i]);
        children[i].emit('close', 1, null);
    }
    await until(() => children.length === 9);
    assert.deepEqual(delays, [1, 2, 4, 8, 16, 32, 60, 60]);
    await supervisor.stop();
});
test('watchdog signals only its own detached process group', async () => {
    const { supervisor, children, signals } = fixture({ watchdogMs: 5, watchdogInterval: 5 });
    supervisor.start();
    await until(() => signals.length > 0);
    assert.ok(signals.every((x) => x.pid === children[0].pid));
    assert.equal(signals[0].signal, 'SIGTERM');
    await supervisor.stop();
});
test('oversized responses are rejected and malformed JSON cannot execute anything', async () => {
    const { supervisor, children, faults } = fixture();
    supervisor.start();
    children[0].stdout.write(Buffer.from('this is not JSON\n'));
    assert.equal(faults.length, 0);
    children[0].stdout.write(Buffer.alloc(512001, 120));
    await until(() => faults.length === 1);
    assert.match(faults[0].message, /size limit/);
    await supervisor.stop();
});
test('stop cancels a scheduled restart and a retired process is never signalled later', async () => {
    const { supervisor, children, signals } = fixture({ delay: () => 30 });
    supervisor.start();
    children[0].emit('close', 1, null);
    await supervisor.stop();
    await pause(40);
    assert.equal(children.length, 1);
    const count = signals.length;
    supervisor.terminate(children[0], 'SIGKILL');
    assert.equal(signals.length, count);
});
