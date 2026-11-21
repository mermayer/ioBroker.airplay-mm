'use strict';
const path = require('node:path');
const fs = require('node:fs/promises');
function validateKey(key) {
    if (typeof key !== 'string' || (key !== '' && !/^[\x21-\x7e]{8,512}$/.test(key)))
        throw new Error('Enter a valid Speechify API key.');
    return key;
}
async function readKey(root, adapter) {
    const file = path.join(root, 'speechify-key.json');
    try {
        const stat = await fs.lstat(file);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 5000) throw new Error();
        const value = JSON.parse(await fs.readFile(file, 'utf8'));
        if (typeof value.encrypted !== 'string') throw new Error();
        return value.encrypted ? validateKey(adapter.decrypt(value.encrypted)) : '';
    } catch (error) {
        if (error.code === 'ENOENT') return '';
        throw new Error('The saved Speechify key could not be decrypted. Enter it again.');
    }
}
async function saveKey(root, adapter, key) {
    validateKey(key);
    const file = path.join(root, 'speechify-key.json'),
        temporary = file + '.tmp';
    for (const candidate of [file, temporary])
        try {
            if ((await fs.lstat(candidate)).isSymbolicLink()) throw new Error('Unexpected link at key file.');
        } catch (error) {
            if (error.code !== 'ENOENT') throw error;
        }
    if (typeof adapter.encrypt !== 'function') throw new Error('ioBroker encryption is unavailable.');
    const encrypted = key ? adapter.encrypt(key) : '';
    if (key && (!encrypted || encrypted === key)) throw new Error('The API key could not be encrypted.');
    await fs.writeFile(temporary, JSON.stringify({ version: 1, encrypted }), { mode: 0o600 });
    await fs.chmod(temporary, 0o600);
    await fs.rename(temporary, file);
}
module.exports = { validateKey, readKey, saveKey };
