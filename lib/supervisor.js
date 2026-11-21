'use strict';
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const { StringDecoder } = require('node:string_decoder');
const path = require('node:path');

class Supervisor extends EventEmitter {
    constructor(root, packageRoot, options = {}) {
        super();
        this.root = root;
        this.packageRoot = packageRoot;
        this.speechifyApiKey = options.speechifyApiKey || '';
        this.spawnFn = options.spawn || spawn;
        this.signalGroup = options.signalGroup || ((pid, signal) => process.kill(-pid, signal));
        this.delay = options.delay || ((retry) => Math.min(60000, 2000 * 2 ** Math.min(retry, 5)));
        this.requestTimeout = options.requestTimeout ?? 20000;
        this.watchdogMs = options.watchdogMs ?? 30000;
        this.watchdogInterval = options.watchdogInterval ?? 5000;
        this.pending = new Map();
        this.child = null;
        this.stopping = false;
        this.ready = false;
        this.retry = 0;
        this.sequence = 0;
    }
    start() {
        if (this.stopping || this.child || this.restartTimer) return;
        this.startedAt = Date.now();
        this.lastHeartbeat = Date.now();
        this.ready = false;
        let child;
        try {
            child = this.spawnFn(
                path.join(this.root, 'venv', 'bin', 'python'),
                ['-u', path.join(this.packageRoot, 'worker', 'server.py'), '--root', this.root],
                {
                    cwd: this.root,
                    detached: true,
                    windowsHide: true,
                    stdio: ['pipe', 'pipe', 'pipe'],
                    env: {
                        ...process.env,
                        AIRPLAY_SPEECHIFY_API_KEY: this.speechifyApiKey,
                        AIRPLAY_NODE_BINARY: process.execPath,
                        PYTHONUNBUFFERED: '1',
                        PYTHONUTF8: '1',
                        PYTHONIOENCODING: 'utf-8',
                        OPENBLAS_NUM_THREADS: '1',
                        OMP_NUM_THREADS: '2',
                    },
                },
            );
        } catch (error) {
            this.emit('fault', error);
            this.schedule();
            return;
        }
        this.child = child;
        const decoder = new StringDecoder('utf8');
        let buffer = '',
            finalized = false;
        child.stdout.on('data', (chunk) => {
            if (child !== this.child || finalized) return;
            buffer += decoder.write(chunk);
            if (buffer.length > 512000) {
                this.emit('fault', new Error('Speech service response exceeds size limit.'));
                this.terminate(child, 'SIGTERM');
                return;
            }
            let index;
            while ((index = buffer.indexOf('\n')) >= 0) {
                const line = buffer.slice(0, index);
                buffer = buffer.slice(index + 1);
                let message;
                try {
                    message = JSON.parse(line);
                } catch {
                    continue;
                }
                if (message.event === 'ready') {
                    this.ready = true;
                    this.lastHeartbeat = Date.now();
                    this.emit('ready', message);
                } else if (message.event === 'status' && message.state && typeof message.state === 'object') {
                    this.lastHeartbeat = Date.now();
                    if (Date.now() - this.startedAt > 30000) this.retry = 0;
                    this.emit('status', message.state);
                } else if (message.event === 'log') this.emit('log', message);
                else if (message.event === 'fatal') {
                    this.ready = false;
                    this.emit('fault', new Error(String(message.error).slice(0, 2000)));
                } else if (this.pending.has(message.id)) {
                    const p = this.pending.get(message.id);
                    this.pending.delete(message.id);
                    clearTimeout(p.timer);
                    message.ok
                        ? p.resolve(message.result)
                        : p.reject(new Error(String(message.error).slice(0, 2000)));
                }
            }
        });
        child.stderr.on('data', (chunk) =>
            this.emit('log', { level: 'debug', message: chunk.toString('utf8').slice(0, 1200) }),
        );
        child.stdin.on('error', (error) => {
            if (!this.stopping) this.emit('fault', error);
        });
        child.on('error', (error) => {
            if (!this.stopping) this.emit('fault', error);
        });
        child.once('close', (code, signal) => {
            if (finalized) return;
            finalized = true;
            // Kill only this detached worker's remaining audio children, before retiring its group.
            this.terminate(child, 'SIGKILL');
            child.retired = true;
            if (this.child === child) this.child = null;
            this.ready = false;
            clearInterval(this.watchdog);
            clearTimeout(this.forceTimer);
            this.rejectPending('Speech service was stopped.');
            this.emit('offline', { code, signal });
            if (!this.stopping) this.schedule();
        });
        this.watchdog = setInterval(() => {
            if (
                !this.stopping &&
                this.child === child &&
                Date.now() - this.lastHeartbeat > this.watchdogMs &&
                !this.forceTimer
            ) {
                this.ready = false;
                this.emit('fault', new Error('Speech service is no longer responding; restarting safely.'));
                this.terminate(child, 'SIGTERM');
                this.forceTimer = setTimeout(() => this.terminate(child, 'SIGKILL'), 1500);
            }
        }, this.watchdogInterval);
    }
    schedule() {
        if (this.stopping || this.restartTimer) return;
        const delay = this.delay(this.retry++);
        this.emit('retry', delay);
        this.restartTimer = setTimeout(() => {
            this.restartTimer = null;
            this.start();
        }, delay);
    }
    terminate(child, signal) {
        if (!child || child.retired || !Number.isInteger(child.pid) || child.pid <= 1) return;
        try {
            this.signalGroup(child.pid, signal);
        } catch (error) {
            if (error.code !== 'ESRCH') child.kill(signal);
        }
    }
    request(action, data) {
        if (!this.ready || !this.child?.stdin.writable || this.stopping)
            return Promise.reject(new Error('Local speech output is not ready yet.'));
        if (this.pending.size >= 16)
            return Promise.reject(new Error('Too many concurrent interface requests.'));
        const child = this.child,
            id = String(++this.sequence);
        return new Promise((resolve, reject) => {
            const timer = setTimeout(
                () => {
                    this.pending.delete(id);
                    reject(
                        new Error(
                            'No response to ' + action + '; announcement will not be repeated automatically.',
                        ),
                    );
                },
                action === 'precache' ? 35000 : this.requestTimeout,
            );
            this.pending.set(id, { resolve, reject, timer });
            try {
                child.stdin.write(JSON.stringify({ id, action, data }) + '\n', (error) => {
                    if (error) {
                        clearTimeout(timer);
                        this.pending.delete(id);
                        reject(error);
                    }
                });
            } catch (error) {
                clearTimeout(timer);
                this.pending.delete(id);
                reject(error);
            }
        });
    }
    rejectPending(reason) {
        for (const p of this.pending.values()) {
            clearTimeout(p.timer);
            p.reject(new Error(reason));
        }
        this.pending.clear();
    }
    async stop() {
        this.stopping = true;
        this.ready = false;
        clearTimeout(this.restartTimer);
        clearInterval(this.watchdog);
        clearTimeout(this.forceTimer);
        this.restartTimer = null;
        this.rejectPending('Adapter is stopping.');
        const child = this.child;
        if (!child) return;
        await new Promise((resolve) => {
            let done = false;
            const finish = () => {
                if (!done) {
                    done = true;
                    clearTimeout(timer);
                    resolve();
                }
            };
            const timer = setTimeout(() => {
                this.terminate(child, 'SIGKILL');
                finish();
            }, 2200);
            child.once('close', finish);
            try {
                child.stdin.end();
            } catch {}
            this.terminate(child, 'SIGTERM');
        });
    }
}
module.exports = { Supervisor };
