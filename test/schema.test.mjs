import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const Ajv = require('ajv');
const cases = [
    ['Admin JSON configuration', 'config-schema.json', '../admin/jsonConfig.json'],
    ['ioBroker adapter metadata', 'io-schema.json', '../io-package.json'],
];
for (const [name, schemaFile, dataFile] of cases)
    test(name + ' conforms to the official schema', async () => {
        const schema = JSON.parse(await fs.readFile(new URL(schemaFile, import.meta.url), 'utf8'));
        const data = JSON.parse(await fs.readFile(new URL(dataFile, import.meta.url), 'utf8'));
        const ajv = new Ajv({ strict: false, allErrors: true, validateFormats: false });
        const valid = ajv.validate(schema, data);
        assert.equal(valid, true, JSON.stringify(ajv.errors, null, 2));
    });
