import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
export const interfaces = {
    eth0: [{ family: 'IPv4', address: '10.0.0.210', cidr: '10.0.0.210/24', internal: false }],
    lo: [{ family: 'IPv4', address: '127.0.0.1', cidr: '127.0.0.1/8', internal: true }],
};
export const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export async function until(predicate, timeout = 1000) {
    const end = Date.now() + timeout;
    while (!predicate()) {
        if (Date.now() > end) throw new Error('Test timed out');
        await pause(2);
    }
}
export class FakeAdapter {
    constructor(config = {}) {
        this.instance = 0;
        this.namespace = 'airplay-mm.0';
        this.config = config;
        this.objects = new Map();
        this.states = new Map();
        this.writes = [];
        this.foreignWrites = [];
        this.foreignObjectEdits = [];
        this.subscriptions = [];
        this.logs = [];
        this.messages = [];
        this.log = Object.fromEntries(
            ['info', 'warn', 'error', 'debug'].map((level) => [
                level,
                (message) => this.logs.push({ level, message }),
            ]),
        );
    }
    async setObjectNotExistsAsync(id, obj) {
        if (!this.objects.has(id)) this.objects.set(id, structuredClone(obj));
    }
    async extendObjectAsync(id, obj) {
        const current = this.objects.get(id);
        this.objects.set(id, { ...current, ...obj, common: { ...current?.common, ...obj.common } });
    }
    async extendForeignObjectAsync(id, obj) {
        this.foreignObjectEdits.push({ id, obj: structuredClone(obj) });
        await this.extendObjectAsync(id, obj);
    }
    async getStateAsync(id) {
        return this.states.get(id) || null;
    }
    async getForeignStateAsync(id) {
        return this.states.get(id) || null;
    }
    async setStateAsync(id, state) {
        this.states.set(id, { ...state });
        this.writes.push({ id, ...state });
    }
    async setForeignStateAsync(id, state) {
        this.states.set(id, { ...state });
        this.foreignWrites.push({ id, ...state });
    }
    async getForeignObjectAsync(id) {
        return this.objects.get(id) || null;
    }
    async subscribeStatesAsync(id) {
        this.subscriptions.push({ own: id });
    }
    async subscribeForeignStatesAsync(id) {
        this.subscriptions.push({ foreign: id });
    }
    sendTo(from, command, value, callback) {
        this.messages.push({ from, command, value, callback });
    }
}
export class FakeSupervisor extends EventEmitter {
    constructor() {
        super();
        this.requests = [];
        this.settings = { target: 'receiver-lg', volume: 15 };
        this.ready = false;
        this.stops = 0;
    }
    start() {
        this.ready = true;
        this.emit('ready', { url: 'http://10.0.0.210:8098/' });
    }
    async request(action, data) {
        if (!this.ready) throw new Error('Not connected');
        this.requests.push({ action, data: structuredClone(data) });
        if (action === 'settings') {
            Object.assign(this.settings, data);
            if (data.targets) this.settings.target = data.targets[0];
            else if (Object.hasOwn(data, 'target')) delete this.settings.targets;
            return structuredClone(this.settings);
        }
        return { id: 'job-1', status: 'queued' };
    }
    async stop() {
        this.stops++;
        this.ready = false;
        this.emit('offline', {});
    }
}
export function childProcess() {
    const child = new EventEmitter();
    child.pid = 23456;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new PassThrough();
    child.kill = () => {};
    return child;
}
