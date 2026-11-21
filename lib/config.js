'use strict';
const os = require('node:os');
const net = require('node:net');
const {kinds:deviceKinds}=require('../web/device-icons.js');

function privateIPv4(value) {
    if (net.isIP(value) !== 4) return false;
    const [a, b] = value.split('.').map(Number);
    return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || a === 127;
}
function inNetwork(address, cidr) {
    const [base, bits] = cidr.split('/');
    const integer = value => value.split('.').reduce((n, part) => (n * 256 + Number(part)) >>> 0, 0);
    const mask = bits === '0' ? 0 : (0xffffffff << (32 - Number(bits))) >>> 0;
    return (integer(address) & mask) === (integer(base) & mask);
}
function normalizeConfig(native = {}, instance = 0, imported = {}, interfaces = os.networkInterfaces()) {
    if (native.importPrototype === false || instance !== 0 || native.schemaVersion >= 2) imported = {};
    const local = Object.values(interfaces).flat().filter(x => x.family === 'IPv4' && privateIPv4(x.address));
    let bind = native.bind || imported.host || local.find(x => !x.internal)?.address;
    if (!bind || !privateIPv4(bind) || !local.some(x => x.address === bind)) {
        throw new Error('Select an existing local IPv4 address as the network address.');
    }
    const network = native.network || imported.network || local.find(x => x.address === bind)?.cidr;
    const [base, bits, extra] = String(network || '').split('/');
    if (!privateIPv4(base) || !/^\d{1,2}$/.test(bits || '') || Number(bits) < 8 || Number(bits) > 32 || extra !== undefined || !inNetwork(bind, network)) {
        throw new Error('The discovery network must be a local IPv4 network containing the selected address (e.g. 192.168.1.0/24).');
    }
    const port = native.port ?? 8098 + instance;
    const maxVolume = native.maxVolume ?? 40;
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid port (1024 to 65535).');
    if (!Number.isInteger(maxVolume) || maxVolume < 1 || maxVolume > 100) throw new Error('Invalid maximum volume (1 to 100).');
    const defaultMode = native.defaultMode || 'native-ntp';
    const modes = new Set(['native-ntp', 'native', 'auto', '1', '2']);
    if (!modes.has(defaultMode)) throw new Error('Unknown AirPlay transport mode.');
    const seedHosts = native.schemaVersion >= 2 ? native.seedHosts ?? [] : native.seedHosts?.length ? native.seedHosts : imported.seedHosts || [];
    if (!Array.isArray(seedHosts) || seedHosts.length > 32 || seedHosts.some(x => typeof x !== 'string' || !privateIPv4(x) || !inNetwork(x, network))) throw new Error('Up to 32 receiver addresses from the discovery network are allowed.');
    const devices = native.schemaVersion >= 2 ? native.devices ?? [] : native.devices?.length ? native.devices : imported.devices || [];
    if (!Array.isArray(devices) || devices.length > 32) throw new Error('Up to 32 preset receivers are allowed.');
    const mapped = {}, presets = [], seen = new Set();
    for (const row of devices) {
        if (!row || !privateIPv4(row.address) || !inNetwork(row.address, network) || seen.has(row.address)) throw new Error('Preset receivers require unique addresses within the discovery network.');
        const mode = row.mode || imported.modes?.[row.address] || defaultMode;
        if (!modes.has(mode)) throw new Error('Unknown receiver transport mode.');
        if (typeof row.name !== 'string' || !row.name.trim() || row.name.length > 80 || /[\x00-\x1f]/.test(row.name)) throw new Error('Receiver names must contain 1 to 80 characters.');
        const kind = row.kind || 'speaker';
        if (!deviceKinds.includes(kind)) throw new Error('Unknown receiver type.');
        seen.add(row.address); mapped[row.address] = mode;
        presets.push({address: row.address, name: row.name.trim(), kind});
    }
    for (const [address, mode] of Object.entries(imported.modes || {})) {
        if (privateIPv4(address) && inNetwork(address, network) && modes.has(mode) && !mapped[address]) mapped[address] = mode;
    }
    if (instance !== 0 && native.legacyBridge) throw new Error('Legacy states can only be imported by instance 0.');
    const sayitInstances=native.sayitInstances??[];
    const speechProvider=native.speechProvider??'piper';
    const piperVoice=native.piperVoice??'de_DE-thorsten-medium';
    const airplayPreRollEnabled=native.airplayPreRollEnabled??true;
    const airplayPreRollMs=native.airplayPreRollMs??700;
    const keepAliveEnabled=native.keepAliveEnabled??true;
    const keepAliveSeconds=native.keepAliveSeconds??300;
    if(typeof keepAliveEnabled!=='boolean'||!Number.isInteger(keepAliveSeconds)||keepAliveSeconds<0||keepAliveSeconds>3600)throw new Error('Persistent connections: choose a duration between 0 and 3600 seconds.');
    if(typeof airplayPreRollEnabled!=='boolean')throw new Error('Pre-roll must be enabled or disabled.');
    if(!Number.isInteger(airplayPreRollMs)||airplayPreRollMs<0||airplayPreRollMs>2000)throw new Error('Pre-roll must be between 0 and 2000 milliseconds.');
    if(!['de_DE-thorsten-medium','de_DE-ramona-low'].includes(piperVoice))throw new Error('Unknown local voice.');
    if(!['piper','sayit-google','speechify'].includes(speechProvider))throw new Error('Unknown speech provider.');
    const alexaLearningEnabled=native.alexaLearningEnabled??false;
    const alexaInstances=native.alexaInstances??['alexa2.0'];
    if(typeof alexaLearningEnabled!=='boolean'||!Array.isArray(alexaInstances)||alexaInstances.length>8||alexaInstances.some(x=>typeof x!=='string'||!/^alexa2\.\d+$/.test(x))||new Set(alexaInstances).size!==alexaInstances.length)throw new Error('Enter distinct Alexa instances such as alexa2.0 for learning mode.');
    if(!Array.isArray(sayitInstances)||sayitInstances.length>8||sayitInstances.some(x=>typeof x!=='string'||!/^sayit\.\d+$/.test(x))||new Set(sayitInstances).size!==sayitInstances.length)throw new Error('Enter up to eight distinct SayIt instances such as sayit.0.');
    return {host: bind, port, network, seedHosts: [...new Set([...seedHosts, ...presets.map(x => x.address)])],
        presets, modes: mapped, defaultMode, maxVolume, importPrototype: instance === 0 && native.importPrototype !== false,
        legacyBridge: instance === 0 && native.legacyBridge !== false,sayitInstances:[...sayitInstances],speechProvider,piperVoice,airplayPreRollEnabled,airplayPreRollMs,keepAliveEnabled,keepAliveSeconds,alexaLearningEnabled,alexaInstances:[...alexaInstances]};
}
module.exports = {normalizeConfig, privateIPv4, inNetwork};
