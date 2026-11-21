import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { format, resolveConfig } from 'prettier';

const root = new URL('../', import.meta.url);
const settings = await fs.readFile(new URL('admin/settings.html', root), 'utf8');
const parts = settings.split('<!-- SPEECH -->');
if (parts.length !== 2) {
    throw new Error('Expected one Admin settings insertion marker.');
}
const [navigation, panel] = parts;
let html = await fs.readFile(new URL('web/index.html', root), 'utf8');

function replaceRequired(pattern, replacement) {
    if (!pattern.test(html)) {
        throw new Error(`Admin source marker was not found: ${pattern}`);
    }
    html = html.replace(pattern, replacement);
}

html = html.replaceAll('href="/icon.svg"', 'href="./airplay-mm.svg"');
html = html.replaceAll('src="/icon.svg"', 'src="./airplay-mm.svg"');
html = html.replaceAll('src="/device-icons.js"', 'src="./device-icons.js"');
replaceRequired(
    /<link rel="stylesheet" href="\/app\.css"\s*\/?>/,
    '<link rel="stylesheet" href="./app.css">\n<link rel="stylesheet" href="./admin.css">',
);
replaceRequired(
    /<script src="\/app\.js" defer><\/script>/,
    '<script src="./config-model.js" defer></script>\n<script src="./admin.js" defer></script>\n<script src="./app.js" defer></script>',
);
replaceRequired(
    /<main>/,
    `${navigation}\n<main>\n<section id="panel-speech" role="tabpanel" aria-labelledby="tab-speech">`,
);
replaceRequired(/<\/main>/, `</section>\n${panel}\n</main>`);
replaceRequired(
    /<div class="workspace">/,
    '<p id="adapter-error" class="notice" role="alert" hidden></p>\n<div class="setup-actions"><button type="button" class="secondary compact" id="setup-retry" hidden>Retry setup</button></div>\n<div class="workspace">',
);

const output = new URL('admin/index_m.html', root);
const options = await resolveConfig(fileURLToPath(output));
await fs.writeFile(output, await format(html, { ...options, parser: 'html' }));
for (const file of ['app.js', 'app.css', 'device-icons.js']) {
    await fs.copyFile(new URL(`web/${file}`, root), new URL(`admin/${file}`, root));
}
console.log('Integrated Admin interface built.');
