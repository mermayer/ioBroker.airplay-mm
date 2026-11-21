'use strict';
function alexaSource(id, instances) {
    const match = /^(alexa2\.\d+)\.Echo-Devices\.[^.]+\.Commands\.(speak|announcement|ssml)$/.exec(id);
    return !!match && instances.includes(match[1]);
}
function alexaText(value) {
    if (typeof value !== 'string' || value.length > 10000)
        throw new Error('Alexa text is invalid or too long.');
    let text = value.replace(/^\s*(?:100|[0-9]{1,2});/, '');
    text = text
        .replace(/<audio\b[^>]*\/>|<audio\b[^>]*>[\s\S]*?<\/audio>/gi, '')
        .replace(/<script\b[^>]*>[\s\S]*?<\/script>|<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
        .replace(/<break\b[^>]*\/?\s*>/gi, ' ')
        .replace(/<[^>]*>/g, '')
        .replace(/&#(x[0-9a-f]+|\d+);/gi, (entity, n) => {
            const c = n[0].toLowerCase() === 'x' ? parseInt(n.slice(1), 16) : Number(n);
            return c > 0 && c <= 0x10ffff && !(c >= 0xd800 && c <= 0xdfff) ? String.fromCodePoint(c) : entity;
        })
        .replace(
            /&(?:amp|lt|gt|quot|apos|nbsp);/gi,
            (entity) =>
                ({ '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'", '&nbsp;': ' ' })[
                    entity.toLowerCase()
                ],
        )
        .replace(/;/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .normalize('NFC');
    if (!text) return null;
    if (Array.from(text).length > 500 || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text))
        throw new Error('Alexa learning text exceeds 500 characters or contains control characters.');
    return text;
}
module.exports = { alexaSource, alexaText };
