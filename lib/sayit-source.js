'use strict';
// Use the installed SayIt generator, without starting an adapter or a player.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const CACHE_PREFIX = 'sayit-google-v1:de:';
const keyFor = (text) =>
    createHash('sha256')
        .update(CACHE_PREFIX + text)
        .digest('hex');

function loadGenerator() {
    let packageFile;
    try {
        packageFile = require.resolve('iobroker.sayit/package.json');
    } catch {
        throw new Error('SayIt is not installed. Install the sayit adapter on the same ioBroker host.');
    }
    const metadata = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
    if (metadata.name !== 'iobroker.sayit') throw new Error('Unexpected SayIt package.');
    const folder = path.dirname(packageFile);
    const file = ['build/lib/text2speech.js', 'lib/text2speech.js']
        .map((name) => path.join(folder, name))
        .find((name) => fs.existsSync(name));
    if (!file)
        throw new Error(
            'This SayIt version does not provide the required generation interface. SayIt 5.3.2 is supported.',
        );
    const exported = require(file),
        Generator = exported.default || exported;
    if (
        typeof Generator !== 'function' ||
        typeof Generator.prototype.sayItGetSpeech !== 'function' ||
        typeof Generator.splitText !== 'function' ||
        typeof Generator.getMaxTextLength !== 'function'
    )
        throw new Error('The SayIt interface has changed. Check the adapter version.');
    return { Generator, version: metadata.version };
}

function validateRoot(root) {
    const resolved = path.resolve(root || '');
    if (resolved !== root || fs.realpathSync(root) !== root || !/^airplay-mm\.\d+$/.test(path.basename(root)))
        throw new Error('Invalid adapter data directory.');
    const cache = path.join(root, 'cache');
    if (fs.realpathSync(cache) !== cache || !fs.statSync(cache).isDirectory())
        throw new Error('Invalid cache folder.');
    return cache;
}

async function generate(root, text, loader = loadGenerator) {
    const cache = validateRoot(root);
    if (
        typeof text !== 'string' ||
        !text.trim() ||
        Array.from(text).length > 500 ||
        /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text)
    )
        throw new Error('Enter an announcement with 1 to 500 characters.');
    const { Generator, version } = loader();
    const output = path.join(cache, keyFor(text) + '.sayit.part.mp3');
    if (fs.lstatSync(output, { throwIfNoEntry: false })?.isSymbolicLink())
        throw new Error('Unexpected link at audio file.');
    const quiet = () => {};
    const adapter = {
        config: { engine: 'de', cache: false },
        namespace: 'airplay-mm',
        log: { debug: quiet, info: quiet, warn: quiet, error: quiet },
    };
    const generator = new Generator(adapter, {
        MP3FILE: output,
        isCached: () => false,
        getCachedFileName: () => output,
        addToQueue: async () => {
            throw new Error('Unexpected additional SayIt request.');
        },
    });
    const limit = Generator.getMaxTextLength('de');
    if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw new Error('Unexpected SayIt text limit.');
    const parts = Generator.splitText(text, limit);
    if (
        !Array.isArray(parts) ||
        !parts.length ||
        parts.length > 32 ||
        parts.some((part) => typeof part !== 'string' || !part || part.length > limit)
    )
        throw new Error('Invalid SayIt text segmentation.');
    const chunks = [];
    let bytes = 0;
    try {
        for (const part of parts) {
            const file = await generator.sayItGetSpeech({
                text: part,
                language: 'de',
                type: 'system',
                testOptions: { engine: 'de' },
            });
            if (file !== output || fs.lstatSync(output).isSymbolicLink())
                throw new Error('Unexpected SayIt audio file.');
            const size = fs.statSync(output).size;
            if (size < 100 || size > 1024 * 1024 || bytes + size > 2 * 1024 * 1024)
                throw new Error('Invalid SayIt audio file size.');
            const data = fs.readFileSync(output);
            bytes += data.length;
            chunks.push(data.toString('base64'));
        }
        return {
            ok: true,
            key: keyFor(text),
            provider: 'sayit-google',
            language: 'de',
            sayitVersion: version,
            chunks,
        };
    } finally {
        fs.rmSync(output, { force: true });
    }
}

async function main() {
    if (process.argv.length === 3 && process.argv[2] === '--check') {
        const { version } = loadGenerator();
        return { ok: true, provider: 'sayit-google', language: 'de', sayitVersion: version };
    }
    if (process.argv.length !== 4 || process.argv[2] !== '--root') throw new Error('Invalid speech call.');
    let data = '';
    for await (const chunk of process.stdin) {
        data += chunk;
        if (Buffer.byteLength(data) > 8192) throw new Error('Speech request is too large.');
    }
    const value = JSON.parse(data);
    if (
        !value ||
        typeof value !== 'object' ||
        Array.isArray(value) ||
        Object.keys(value).some((key) => key !== 'text')
    )
        throw new Error('Invalid speech request.');
    return generate(process.argv[3], value.text);
}
if (require.main === module)
    main()
        .then((result) => process.stdout.write(JSON.stringify(result) + '\n'))
        .catch((error) => {
            // HTTP errors may contain the text/URL. Keep provider diagnostics bounded and neutral.
            const known =
                /SayIt|Adapter-Datenverzeichnis|Cache-Ordner|Ansagetext|Sprachauftrag|Sprachaufruf|Audiodatei|Textgrenze|Textaufteilung/.test(
                    String(error.message),
                );
            process.stdout.write(
                JSON.stringify({
                    ok: false,
                    error: known
                        ? String(error.message).slice(0, 500)
                        : 'SayIt/Google could not generate the text. Check internet access and availability of the speech service.',
                }) + '\n',
            );
            process.exitCode = 1;
        });
module.exports = { generate, keyFor, loadGenerator, validateRoot };
