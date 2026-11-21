'use strict';

function parseSayit(value,maxVolume=40,defaultVolume=15) {
    if(typeof value!=='string'||!value.trim()||value.length>550)throw new Error('Invalid SayIt announcement text.');
    let text=value.trim(),volume=defaultVolume;
    const language=text.match(/^([a-z]{2}(?:[-_][a-z]{2})?);/i);
    if(language){if(!/^de(?:[-_]de)?$/i.test(language[1]))throw new Error('The local voice supports German SayIt announcements.');text=text.slice(language[0].length);}
    const level=text.match(/^(\d{1,3});/);
    if(level){const selected=Number(level[1]);if(selected>100)throw new Error('Invalid SayIt volume.');volume=Math.min(selected,maxVolume);text=text.slice(level[0].length);}
    if(/^(?:https?:\/\/|file:|\/|[a-z]:\\)/i.test(text)||/\.(?:mp3|wav|ogg)$/i.test(text.trim()))throw new Error('SayIt import handles text; audio files and URLs are not played.');
    text=text.trim();if(!text||Array.from(text).length>500||/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text))throw new Error('Use a SayIt text with 1 to 500 characters.');
    return {text,volume};
}

module.exports={parseSayit};
