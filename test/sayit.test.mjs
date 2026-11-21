import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url),
    { parseSayit } = require('../lib/sayit');
test('plain SayIt text and separate default volume remain supported', () => {
    assert.deepEqual(parseSayit('Die Tür ist offen.', 40, 18), { text: 'Die Tür ist offen.', volume: 18 });
});
test('German language and inline volume are parsed and capped', () => {
    assert.deepEqual(parseSayit('de;80;Die Tür ist offen.', 40, 18), {
        text: 'Die Tür ist offen.',
        volume: 40,
    });
    assert.equal(parseSayit('de-DE;10;Hallo').volume, 10);
    assert.equal(parseSayit('0;Hallo').volume, 0);
});
test('normal semicolons inside spoken text are preserved', () => {
    assert.equal(parseSayit('Hallo; bitte nachsehen').text, 'Hallo; bitte nachsehen');
});
test('foreign languages, invalid volumes and file playback are rejected', () => {
    for (const text of [
        'en;Hello',
        '999;Hallo',
        'https://example.org/a.mp3',
        '/tmp/a.wav',
        'file:/tmp/a.mp3',
        '',
        'x'.repeat(501),
    ])
        assert.throws(() => parseSayit(text));
});
