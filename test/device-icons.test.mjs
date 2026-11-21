import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import { interfaces } from './helpers.mjs';
const require = createRequire(import.meta.url),
    icons = require('../web/device-icons.js'),
    { normalizeConfig } = require('../lib/config.js'),
    model = require('../admin/config-model.js');
test('real HomePod and Mac mini model identifiers are recognized despite room-only names', () => {
    assert.equal(
        icons.kind({
            kind: 'speaker',
            name: 'Example room',
            model: 'AudioAccessory11,1',
            modelIdentifier: 'AudioAccessory11,1',
        }),
        'homepod',
    );
    for (const identifier of [
        'AudioAccessory1,1',
        'AudioAccessory5,1',
        'AudioAccessory6,1',
        'AudioAccessory999,1',
    ])
        assert.equal(icons.kind({ name: 'Wohnzimmer', modelIdentifier: identifier }), 'homepod');
    for (const identifier of [
        'Mac14,12',
        'Mac14,3',
        'Mac16,10',
        'Mac16,11',
        'Mac17,16',
        'Mac18,5',
        'Macmini9,1',
    ])
        assert.equal(icons.kind({ name: 'Example room', modelIdentifier: identifier }), 'macmini');
});
test('model metadata wins over ambiguous names and recognizes TV, Apple TV, MacBook and network players', () => {
    for (const [row, expected] of [
        [{ name: 'Wohnzimmer', model: 'Sony_XR-65X95L' }, 'tv'],
        [{ name: 'HomePod', modelIdentifier: 'AppleTV14,1' }, 'appletv'],
        [{ name: 'Büro', modelIdentifier: 'MacBookAir10,1' }, 'laptop'],
        [{ name: 'Büro', modelIdentifier: 'iMac20,1' }, 'computer'],
        [{ name: 'Büro', modelIdentifier: 'Mac99,1' }, 'computer'],
        [{ name: 'NODE ICON', available: false, kind: 'speaker' }, 'streamer'],
        [{ name: 'Mac mini Office', kind: 'speaker' }, 'macmini'],
        [{ name: 'LG_Speaker_SP8YA' }, 'soundbar'],
    ])
        assert.equal(icons.kind(row), expected, JSON.stringify(row));
});
test('explicit symbol overrides preserve existing Soundbar/TV and allow a forced generic speaker', () => {
    for (const kind of icons.kinds.filter((x) => x !== 'speaker'))
        assert.equal(
            icons.kind({ kind, name: 'HomePod', modelIdentifier: 'AudioAccessory11,1' }),
            kind === 'loudspeaker' ? 'speaker' : kind,
        );
    assert.equal(icons.kind({ kind: 'soundbar', name: 'Example Soundbar' }), 'soundbar');
    assert.equal(icons.kind({ kind: 'tv', name: 'Wohnzimmer' }), 'tv');
});
test('unknown or hostile metadata has a neutral fallback and never becomes SVG markup', () => {
    for (const input of [
        null,
        {},
        false,
        { kind: 'constructor' },
        { kind: '__proto__' },
        { kind: '<script>alert(1)</script>', name: '<img src=x onerror=alert(1)>', model: { invalid: true } },
    ]) {
        const kind = icons.kind(input);
        assert.equal(kind, 'speaker');
        assert.ok(icons.svg(kind).startsWith('<svg '));
        assert.doesNotMatch(icons.svg(kind), /script|onerror|javascript:|<img/i);
    }
    assert.equal(icons.svg('constructor'), icons.svg('speaker'));
    for (const kind of Object.keys(icons.labels)) {
        const svg = icons.svg(kind);
        assert.match(svg, /viewBox="0 0 84 64"/);
        assert.match(svg, /aria-hidden="true"/);
        assert.ok(svg.endsWith('</svg>'));
    }
});
test('all manual symbols round-trip through UI, runtime and configuration merge without changing audio options', () => {
    const native = {
        schemaVersion: 2,
        bind: '10.0.0.210',
        network: '10.0.0.0/24',
        speechProvider: 'speechify',
        piperVoice: 'de_DE-ramona-low',
        airplayPreRollMs: 700,
        keepAliveSeconds: 300,
        other: 'unchanged',
    };
    for (const kind of icons.kinds) {
        const draft = model.initial({
            ...native,
            devices: [{ name: 'Example room', address: '10.0.0.225', kind, mode: 'native-ntp' }],
        });
        const validated = model.validate(draft),
            runtime = normalizeConfig(validated, 0, interfaces);
        assert.equal(runtime.presets[0].kind, kind);
        assert.equal(runtime.modes['10.0.0.225'], 'native-ntp');
        assert.equal(runtime.speechProvider, 'speechify');
        assert.equal(runtime.airplayPreRollMs, 700);
        assert.equal(runtime.keepAliveSeconds, 300);
        assert.equal(model.merge(native, native, validated).other, 'unchanged');
    }
});
test('both shipped surfaces load the same self-hosted symbol module before their consumers', async () => {
    const root = new URL('../', import.meta.url);
    for (const folder of ['admin', 'web']) {
        const html = await fs.readFile(
            new URL(folder + '/' + (folder === 'admin' ? 'index_m.html' : 'index.html'), root),
            'utf8',
        );
        assert.ok(html.indexOf('device-icons.js') < html.indexOf('app.js'));
        if (folder === 'admin') assert.ok(html.indexOf('device-icons.js') < html.indexOf('config-model.js'));
    }
    assert.equal(
        await fs.readFile(new URL('admin/device-icons.js', root), 'utf8'),
        await fs.readFile(new URL('web/device-icons.js', root), 'utf8'),
    );
});
