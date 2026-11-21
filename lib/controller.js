'use strict';
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const runtime = require('./runtime');
const { normalizeConfig } = require('./config');
const { Supervisor } = require('./supervisor');
const { parseSayit } = require('./sayit');
const secrets = require('./speechify-key');
const { alexaSource, alexaText } = require('./alexa-learning');
const VERSION = require('../package.json').version;
const STATES = [
    ['text', '', 'string', 'text', true, 'Text for the next announcement'],
    ['say', '', 'string', 'text', true, 'Write text and announce immediately'],
    ['target', '', 'string', 'text', true, 'AirPlay receiver ID'],
    ['targets', '[]', 'string', 'json', true, 'One to eight AirPlay receiver IDs as a JSON list'],
    ['volume', 15, 'number', 'level.volume', true, 'Announcement volume'],
    ['masterGain', 100, 'number', 'level.volume', true, 'Announcement master gain'],
    ['speak', false, 'boolean', 'button', true, 'Start announcement'],
    ['discover', false, 'boolean', 'button', true, 'Find AirPlay receivers'],
    ['cancel', false, 'boolean', 'button', true, 'Cancel active and queued announcements'],
    ['retrySetup', false, 'boolean', 'button', true, 'Retry setup'],
    ['info.connection', false, 'boolean', 'indicator.connected', false, 'Speech output ready'],
    ['info.status', 'Starting', 'string', 'text', false, 'Current status'],
    ['info.lastError', '', 'string', 'text', false, 'Last error'],
    ['info.devices', '[]', 'string', 'json', false, 'Discovered receivers'],
    ['info.history', '[]', 'string', 'json', false, 'Announcement history'],
    ['info.queueLength', 0, 'number', 'value', false, 'Queued announcements'],
    ['info.heartbeat', '', 'string', 'date', false, 'Last response'],
    ['info.panelUrl', '', 'string', 'text.url', false, 'Web interface'],
    ['info.version', VERSION, 'string', 'info.version', false, 'Adapter version'],
    ['info.setup', 'Not configured yet', 'string', 'text', false, 'Local speech setup'],
    ['info.learning', '{}', 'string', 'json', false, 'Alexa learning mode and queue'],
];
const names = new Set(STATES.map((x) => x[0]));

class Controller {
    constructor(adapter, utils, options = {}) {
        this.adapter = adapter;
        this.utils = utils;
        this.options = options;
        this.runtime = options.runtime || runtime;
        this.Supervisor = options.Supervisor || Supervisor;
        this.packageRoot = options.packageRoot || path.resolve(__dirname, '..');
        this.values = { text: '', target: '', targets: [], volume: 15, masterGain: 100 };
        this.previous = new Map();
        this.commandChain = Promise.resolve();
        this.commandCount = 0;
        this.stopping = false;
        this.setupTask = null;
        this.status = null;
        this.commandError = '';
        this.sayitSources = new Set();
        this.sayitStartedAt = Date.now();
        this.preparing = false;
        this.secrets = options.secrets || secrets;
        this.learningBacklog = [];
        this.learningChain = Promise.resolve();
        this.learningStartedAt = Date.now();
    }
    async put(key, value) {
        if (!names.has(key)) throw new Error('Unknown adapter state.');
        if (this.previous.get(key) === value) return;
        await this.adapter.setStateAsync(key, { val: value, ack: true });
        this.previous.set(key, value);
    }
    failure(error) {
        const message = String(error.message || error).slice(0, 2000);
        this.commandError = message;
        this.adapter.log.warn(message);
        this.put('info.lastError', message).catch((e) =>
            this.adapter.log.warn('Error status could not be saved: ' + String(e.message || e)),
        );
    }
    async start() {
        for (const [id, def, type, role, write, name] of STATES) {
            await this.adapter.setObjectNotExistsAsync(id, {
                type: 'state',
                common: {
                    name,
                    type,
                    role,
                    read: true,
                    write,
                    def,
                    ...(id === 'volume'
                        ? { min: 0, max: this.adapter.config.maxVolume ?? 40, unit: '%' }
                        : id === 'masterGain'
                          ? { min: 0, max: 100, unit: '%' }
                          : {}),
                },
                native: {},
            });
            const state = await this.adapter.getStateAsync(id);
            if (!state) await this.put(id, def);
            else if (Object.hasOwn(this.values, id)) {
                if (id === 'targets') {
                    try {
                        this.values.targets = JSON.parse(state.val);
                    } catch {
                        this.values.targets = [];
                    }
                } else this.values[id] = state.val;
            }
        }
        await this.put('info.connection', false);
        await this.put('info.version', VERSION);
        // Never replay an unacknowledged command retained by the state database.
        for (const key of ['say', 'speak', 'discover', 'cancel', 'retrySetup']) {
            this.previous.delete(key);
            await this.put(key, key === 'say' ? '' : false);
        }
        await this.adapter.subscribeStatesAsync('*');
        await this.setup();
    }
    async setup() {
        if (this.stopping || this.setupTask || this.worker) return;
        this.setupTask = this.initialize();
        try {
            await this.setupTask;
        } catch (error) {
            if (!this.stopping) {
                this.failure(error);
                await this.put('info.status', 'Check setup');
                await this.put('info.setup', 'Failed – See the log and info.lastError');
            }
        } finally {
            this.setupTask = null;
        }
    }
    async initialize() {
        const a = this.adapter;
        this.abort = new AbortController();
        const data = this.utils.getAbsoluteInstanceDataDir(a);
        this.root = await this.runtime.assertDirectory(data);
        this.config = normalizeConfig(a.config, a.instance, this.options.interfaces);
        this.speechifyApiKey = '';
        try {
            this.speechifyApiKey = await this.secrets.readKey(this.root, a);
        } catch (error) {
            a.log.warn(error.message);
        }
        if (this.config.alexaLearningEnabled) {
            for (const name of this.config.alexaInstances)
                for (const command of ['speak', 'announcement', 'ssml'])
                    await a.subscribeForeignStatesAsync(name + '.Echo-Devices.*.Commands.' + command);
            a.log.info('Alexa learning enabled: texts are prepared without changing Alexa states.');
        }
        for (const name of this.config.sayitInstances) {
            const object = await a.getForeignObjectAsync('system.adapter.' + name);
            if (!object || object.common?.enabled) {
                a.log.warn(name + ' was not imported: the SayIt instance must be installed and disabled.');
                continue;
            }
            for (const key of ['text', 'cachetext']) {
                const id = name + '.tts.' + key;
                if (await a.getForeignObjectAsync(id)) {
                    this.sayitSources.add(id);
                    await a.subscribeForeignStatesAsync(id);
                }
            }
        }
        if (this.stopping || this.abort.signal.aborted) return;
        await this.put('info.setup', 'Checking and preparing the local runtime');
        const installed = await this.runtime.prepare(this.root, this.packageRoot, this.config, {
            signal: this.abort.signal,
            onLine: (line) => {
                if (line.event === 'setup')
                    this.put('info.setup', String(line.message).slice(0, 500)).catch((error) =>
                        this.failure(error),
                    );
            },
        });
        if (this.stopping) return;
        await a.extendObjectAsync('volume', { common: { max: this.config.maxVolume } });
        await this.put(
            'info.setup',
            this.config.speechProvider === 'speechify'
                ? 'Ready · Speechify Runa, Ramona fallback and AirPlay configured'
                : this.config.speechProvider === 'sayit-google'
                  ? 'Ready · SayIt/Google and local AirPlay sender configured'
                  : 'Ready · Local Piper and AirPlay configured',
        );
        a.log.info('Local speech output verified. Python runtime ready: ' + installed.pythonVersion);
        this.worker = new this.Supervisor(this.root, this.packageRoot, {
            ...this.options.supervisorOptions,
            speechifyApiKey: this.speechifyApiKey,
        });
        this.worker.on('fault', (error) => this.failure(error));
        this.worker.on('ready', (message) => {
            this.commandError = '';
            this.put('info.panelUrl', message.url).catch((error) => this.failure(error));
            this.put('info.connection', true).catch((error) => this.failure(error));
            a.log.info('Web interface ready: ' + message.url);
            this.flushLearning();
        });
        this.worker.on('status', (state) => this.update(state).catch((error) => this.failure(error)));
        this.worker.on('offline', () => {
            this.status = null;
            this.put('info.connection', false).catch((error) => this.failure(error));
        });
        this.worker.on('retry', (delay) => {
            this.put(
                'info.status',
                'Speech service interrupted – Restart in ' + Math.round(delay / 1000) + ' seconds',
            ).catch((error) => this.failure(error));
            a.log.warn('Speech service will restart in ' + Math.round(delay / 1000) + ' seconds.');
        });
        this.worker.on('log', (row) => {
            const level = ['debug', 'info', 'warn', 'error'].includes(row.level) ? row.level : 'debug';
            a.log[level](String(row.message || '').slice(0, 2000));
        });
        this.worker.start();
    }
    async update(state) {
        if (this.stopping) return;
        this.status = state;
        await this.put('info.learning', JSON.stringify(state.learning || {}));
        if (this.learningBacklog.length) this.flushLearning();
        const labels = {
            generating: 'Generating speech',
            connecting: 'Connecting',
            playing: 'Sending announcement',
            queued: 'Queued',
        };
        await this.put('info.connection', !!this.worker?.ready && !!state.ready);
        const error = String(state.lastError || this.commandError || '');
        await this.put(
            'info.status',
            state.active
                ? labels[state.active.status] || state.active.status
                : state.discovering
                  ? 'Searching for receivers'
                  : error
                    ? 'Ready – Last request failed'
                    : 'Ready',
        );
        await this.put('info.lastError', error);
        await this.put('info.devices', JSON.stringify(state.devices || []));
        await this.put('info.history', JSON.stringify(state.history || []));
        await this.put('info.queueLength', Number(state.queued || 0));
        await this.put('info.heartbeat', new Date().toISOString());
        if (state.settings && !this.commandCount) {
            this.values.target = state.settings.target || '';
            this.values.volume = state.settings.volume;
            this.values.masterGain = state.masterGain ?? state.settings.masterGain ?? 100;
            this.values.targets = state.settings.targets || (this.values.target ? [this.values.target] : []);
            await this.put('target', this.values.target);
            await this.put('volume', this.values.volume);
            await this.put('masterGain', this.values.masterGain);
            await this.put('targets', JSON.stringify(this.values.targets));
        }
    }
    enqueue(action) {
        if (this.stopping) return Promise.reject(new Error('Adapter is stopping.'));
        if (this.commandCount >= 16) return Promise.reject(new Error('Too many concurrent commands.'));
        this.commandCount++;
        const next = this.commandChain.then(action);
        this.commandChain = next
            .catch(() => {})
            .finally(() => {
                this.commandCount--;
            });
        return next;
    }
    async speak(data) {
        const request = typeof data === 'string' ? { text: data } : data;
        if (
            !request ||
            typeof request !== 'object' ||
            Array.isArray(request) ||
            Object.keys(request).some(
                (k) => !['text', 'target', 'targets', 'volume', 'requestId'].includes(k),
            )
        )
            throw new Error('Invalid announcement.');
        if (typeof request.text !== 'string' || !request.text.trim() || Array.from(request.text).length > 500)
            throw new Error('Enter a text with 1 to 500 characters.');
        if (!this.worker) throw new Error('Speech output is not ready yet.');
        const defaults =
            Object.hasOwn(request, 'target') || Object.hasOwn(request, 'targets')
                ? {}
                : this.values.targets?.length > 1
                  ? { targets: this.values.targets }
                  : { target: this.values.target };
        const result = await this.worker.request('speak', {
            ...defaults,
            volume: this.values.volume,
            ...request,
            requestId: request.requestId || randomUUID(),
        });
        this.commandError = '';
        return result;
    }
    stateChange(id, state) {
        if (!state || state.ack !== false || this.stopping) return;
        if (this.config?.alexaLearningEnabled && alexaSource(id, this.config.alexaInstances)) {
            if (Number.isFinite(state.ts) && state.ts < this.learningStartedAt) return;
            try {
                const text = alexaText(state.val);
                if (!text) return;
                if (!this.learningBacklog.includes(text)) {
                    if (this.learningBacklog.length >= 256) throw new Error('Learning queue is full.');
                    this.learningBacklog.push(text);
                }
                this.flushLearning();
            } catch (error) {
                this.adapter.log.warn('Alexa learning mode: ' + error.message);
            }
            return;
        }
        if (this.sayitSources.has(id)) {
            if (Number.isFinite(state.ts) && state.ts < this.sayitStartedAt) return;
            this.enqueue(async () => {
                const source = id.slice(0, id.indexOf('.tts.'));
                const object = await this.adapter.getForeignObjectAsync('system.adapter.' + source);
                if (!object || object.common?.enabled)
                    throw new Error(
                        source +
                            ' is enabled; optional SayIt import was skipped to prevent duplicate announcements.',
                    );
                const savedVolume = await this.adapter.getForeignStateAsync(source + '.tts.volume');
                const number = Number(savedVolume?.val);
                const fallback =
                    savedVolume?.val !== null &&
                    savedVolume?.val !== undefined &&
                    savedVolume?.val !== '' &&
                    Number.isFinite(number) &&
                    number >= 0 &&
                    number <= 100
                        ? Math.min(number, this.config.maxVolume)
                        : this.values.volume;
                const parsed = parseSayit(state.val, this.config.maxVolume, fallback);
                if (id.endsWith('.cachetext')) return this.prepareText({ text: parsed.text });
                return this.speak(parsed);
            }).catch((error) => this.failure(error));
            return;
        }
        let key;
        if (id.startsWith(this.adapter.namespace + '.')) key = id.slice(this.adapter.namespace.length + 1);
        if (!STATES.some((row) => row[0] === key && row[4])) return;
        const val = state.val;
        this.enqueue(async () => {
            this.previous.delete(key);
            if (['speak', 'discover', 'cancel', 'retrySetup'].includes(key)) {
                await this.put(key, false);
                if (val !== true) return;
                if (key === 'retrySetup') return this.setup();
                if (key === 'speak') return this.speak(this.values.text);
                if (!this.worker) throw new Error('Speech output is not ready yet.');
                const result = await this.worker.request(key);
                this.commandError = '';
                return result;
            }
            if (key === 'say') {
                await this.put(key, typeof val === 'string' ? val : '');
                return this.speak(val);
            }
            if (key === 'text') {
                if (typeof val !== 'string' || Array.from(val).length > 500)
                    throw new Error('Text must not exceed 500 characters.');
                this.values.text = val;
                return this.put(key, val);
            }
            if (key === 'target' || key === 'targets' || key === 'volume' || key === 'masterGain') {
                if (!this.worker) throw new Error('Speech output is not ready yet.');
                let value = val;
                if (key === 'targets') {
                    try {
                        value = typeof val === 'string' ? JSON.parse(val) : val;
                    } catch {
                        throw new Error('Provide receiver IDs as a JSON list.');
                    }
                }
                const result = await this.worker.request('settings', { [key]: value });
                this.commandError = '';
                this.values.target = result.target;
                this.values.volume = result.volume;
                this.values.masterGain = result.masterGain ?? 100;
                this.values.targets = result.targets || (result.target ? [result.target] : []);
                await this.put('target', result.target);
                await this.put('volume', result.volume);
                await this.put('masterGain', this.values.masterGain);
                return this.put('targets', JSON.stringify(this.values.targets));
            }
        }).catch((error) => this.failure(error));
    }
    message(message) {
        if (!message || typeof message !== 'object') return;
        const reply = (value) => {
            if (message.callback) this.adapter.sendTo(message.from, message.command, value, message.callback);
        };
        if (message.command === 'getPanelUrl') {
            reply(this.previous.get('info.panelUrl') || '');
            return;
        }
        if (message.command === 'setupStatus') {
            reply(this.previous.get('info.setup') || 'Starting');
            return;
        }
        if (message.command === 'precache') {
            this.prepareText(message.message)
                .then((result) => reply({ ok: true, result }))
                .catch((error) => {
                    this.failure(error);
                    reply({ ok: false, error: String(error.message || error) });
                });
            return;
        }
        if (message.command === 'adminStatus') {
            const parse = (key) => {
                try {
                    return JSON.parse(this.previous.get(key) || '[]');
                } catch {
                    return [];
                }
            };
            reply({
                ok: true,
                result: {
                    ...(this.status || {
                        devices: parse('info.devices'),
                        history: parse('info.history'),
                        settings: { target: this.values.target, volume: this.values.volume },
                        queued: 0,
                    }),
                    ready: !!this.worker?.ready && !!this.status?.ready,
                    version: VERSION,
                    setupStatus: this.previous.get('info.setup') || 'Starting',
                    lastError: this.status?.lastError || this.commandError || '',
                    maxVolume: this.config?.maxVolume || this.adapter.config.maxVolume || 40,
                    masterGain: this.status?.masterGain ?? this.values.masterGain,
                    canRetry: !this.setupTask && !this.worker,
                    effectiveConfig: this.config || null,
                    speechifyKeyPresent: !!this.speechifyApiKey,
                },
            });
            return;
        }
        this.enqueue(async () => {
            if (
                ['cacheList', 'cachePlay', 'cacheDelete', 'retryLearning', 'saveSpeechifyKey'].includes(
                    message.command,
                )
            ) {
                if (!/^system\.adapter\.admin\.\d+$/.test(message.from || ''))
                    throw new Error('Manage cache and keys through the adapter interface.');
                if (!this.worker?.ready) throw new Error('Speech service is not ready yet.');
                if (message.command === 'saveSpeechifyKey') {
                    const key = secrets.validateKey(message.message?.key);
                    await this.secrets.saveKey(this.root, this.adapter, key);
                    this.speechifyApiKey = key;
                    this.worker.speechifyApiKey = key;
                    return this.worker.request('setSpeechifyKey', { key });
                }
                return this.worker.request(message.command, message.message || {});
            }
            if (message.command === 'speak' || message.command === 'say') return this.speak(message.message);
            if (message.command === 'getStatus') return this.worker?.request('status') ?? { ready: false };
            if (message.command === 'settings') {
                if (!this.worker) throw new Error('Speech output is not ready yet.');
                const result = await this.worker.request('settings', message.message);
                this.commandError = '';
                this.values.target = result.target;
                this.values.volume = result.volume;
                this.values.masterGain = result.masterGain ?? 100;
                this.values.targets = result.targets || (result.target ? [result.target] : []);
                await this.put('target', result.target);
                await this.put('volume', result.volume);
                await this.put('masterGain', this.values.masterGain);
                await this.put('targets', JSON.stringify(this.values.targets));
                return result;
            }
            if (
                ['pairStart', 'pairFinish', 'pairCancel', 'forgetPairing', 'disconnect'].includes(
                    message.command,
                )
            ) {
                if (!/^system\.adapter\.admin\.\d+$/.test(message.from || ''))
                    throw new Error('Manage pairings and connections through the adapter interface.');
                if (!this.worker) throw new Error('Speech output is not ready yet.');
                return this.worker.request(message.command, message.message);
            }
            if (message.command === 'validateConfig') {
                const data = message.message;
                if (!data || typeof data !== 'object' || Array.isArray(data))
                    throw new Error('Invalid configuration.');
                normalizeConfig(data, this.adapter.instance, this.options.interfaces);
                return { valid: true };
            }
            if (message.command === 'retrySetup') {
                await this.setup();
                return { started: true };
            }
            if (['discover', 'cancel'].includes(message.command) && this.worker) {
                const result = await this.worker.request(message.command);
                this.commandError = '';
                return result;
            }
            throw new Error('Unknown command or speech output is not ready yet.');
        })
            .then((result) => reply({ ok: true, result }))
            .catch((error) => {
                this.failure(error);
                reply({ ok: false, error: String(error.message || error).slice(0, 2000) });
            });
    }
    async stop() {
        if (this.stopTask) return this.stopTask;
        this.stopping = true;
        this.abort?.abort();
        this.stopTask = (async () => {
            if (this.worker) await this.worker.stop();
            await this.put('info.connection', false);
            await this.put('info.status', 'Stopped');
        })();
        return this.stopTask;
    }
    async prepareText(data) {
        if (this.stopping || !this.worker?.ready) throw new Error('Speech output is not ready yet.');
        if (this.preparing) throw new Error('A text is already being prepared.');
        this.preparing = true;
        try {
            const result = await this.worker.request('precache', data);
            this.commandError = '';
            return result;
        } finally {
            this.preparing = false;
        }
    }
    flushLearning() {
        if (this.stopping || !this.worker?.ready || this.learningFlushing) return;
        this.learningFlushing = true;
        let failed = false;
        this.learningChain = this.learningChain
            .then(async () => {
                while (this.learningBacklog.length && this.worker?.ready && !this.stopping) {
                    const text = this.learningBacklog[0];
                    try {
                        await this.worker.request('learn', { text });
                        this.learningBacklog.shift();
                    } catch (error) {
                        failed = true;
                        this.adapter.log.warn(
                            'Alexa learning mode: ' + String(error.message || error).slice(0, 500),
                        );
                        break;
                    }
                }
            })
            .catch((error) => {
                failed = true;
                this.adapter.log.warn('Learning queue: ' + String(error.message || error).slice(0, 500));
            })
            .finally(() => {
                this.learningFlushing = false;
                if (this.learningBacklog.length && !failed) this.flushLearning();
            });
    }
}
module.exports = { Controller, STATES };
