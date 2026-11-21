(function(scope){
  'use strict';
  const options=Object.freeze([
    ['speaker','Automatic'],['loudspeaker','Speaker'],['homepod','HomePod / HomePod mini'],
    ['soundbar','Soundbar'],['streamer','Network streamer'],['macmini','Mac mini'],
    ['laptop','MacBook / Notebook'],['computer','Computer / iMac'],['tv','TV'],['appletv','Apple TV'],
  ].map(row=>Object.freeze(row)));
  const drawings=Object.freeze({
    speaker:'<rect x="25" y="5" width="34" height="54" rx="5"/><circle cx="42" cy="19" r="4"/><circle cx="42" cy="43" r="9"/>',
    soundbar:'<rect x="4" y="26" width="76" height="16" rx="3"/><path d="M35 34h14"/>',
    homepod:'<path d="M17 26c0-13 10-21 25-21s25 8 25 21v10c0 14-9 23-25 23s-25-9-25-23Z"/><ellipse cx="42" cy="18" rx="20" ry="8"/><path d="M31 18h6m10 0h6m-3-3v6M22 33c10 5 30 5 40 0M22 42c10 5 30 5 40 0M28 50c8 3 20 3 28 0"/>',
    macmini:'<path d="m12 25 9-11h42l9 11v25H12Z"/><path d="M12 25h60M24 39h7m7 0h7"/><circle cx="61" cy="39" r="1.5"/>',
    laptop:'<rect x="16" y="8" width="52" height="34" rx="3"/><path d="m16 42-9 11c0 3 3 4 6 4h58c3 0 6-1 6-4l-9-11M7 53h70M34 48h16"/>',
    computer:'<rect x="10" y="5" width="64" height="43" rx="3"/><path d="M10 39h64M36 48v10m12-10v10M28 59h28"/><circle cx="42" cy="44" r="1"/>',
    tv:'<rect x="6" y="9" width="72" height="41" rx="3"/><path d="M42 50v9M26 59h32"/>',
    appletv:'<rect x="12" y="12" width="41" height="41" rx="9"/><rect x="62" y="5" width="13" height="54" rx="6"/><circle cx="68.5" cy="18" r="4"/><path d="M66 32h5m-5 8h5M27 30l12-7v14z"/>',
    streamer:'<rect x="7" y="17" width="70" height="33" rx="4"/><rect x="16" y="25" width="28" height="16" rx="1"/><circle cx="60" cy="33" r="8"/><path d="M13 53h9m40 0h9M25 30l8 3-8 3z"/>',
  });
  const labels=Object.freeze({speaker:'Speaker',soundbar:'Soundbar',homepod:'HomePod / HomePod mini',macmini:'Mac mini',laptop:'MacBook / Notebook',computer:'Computer / iMac',tv:'TV',appletv:'Apple TV',streamer:'Network streamer'});
  // Apple model identifiers: https://support.apple.com/en-us/102852
  const macMinis=new Set(['mac14,3','mac14,12','mac16,10','mac16,11','mac17,16','mac18,5']);
  const text=value=>typeof value==='string'?value.slice(0,256):'';
  function identify(value){
    value=value.replaceAll('_',' ');
    if(/home\s*pod|audioaccessory/i.test(value))return 'homepod';
    if(/mac\s*mini/i.test(value)||value.toLowerCase().split(/[\s;]+/).some(x=>macMinis.has(x)))return 'macmini';
    if(/mac\s*book|\bnotebook\b|\blaptop\b/i.test(value))return 'laptop';
    if(/\bimac(?:\d|\b)|\bmac\s*(?:pro|studio)(?:\d|\b)/i.test(value))return 'computer';
    if(/apple\s*tv/i.test(value))return 'appletv';
    if(/\bbravia\b|\b(?:XR|KD|KDL|KE)[- ]?\d{2,3}[a-z0-9]+|\bOLED\d{2}|\b(?:QN|QE|UE)\d{2}|\bTX-\d{2}|\bfernseher\b|\btelevision\b|\btv\b/i.test(value))return 'tv';
    if(/soundbar|\bLG[_ -]Speaker[_ -]SP\d+Y[A-Z0-9]*/i.test(value))return 'soundbar';
    if(/\bNODE(?:[ _-]+ICON)?\b|\bBLUESOUND\b|\bPOWERNODE\b|\bWiiM\b|\bHEOS[ _-]*LINK\b/i.test(value))return 'streamer';
    return '';
  }
  function kind(device){
    if(!device||typeof device!=='object')return 'speaker';
    if(device.kind==='loudspeaker')return 'speaker';
    if(device.kind!=='speaker'&&Object.hasOwn(drawings,device.kind))return device.kind;
    const model=[text(device.modelIdentifier),text(device.model)].join(' ');
    return identify(model)||identify([text(device.name),text(device.advertisedName)].join(' '))||(/\bMac\d+,\d+\b/i.test(model)?'computer':'speaker');
  }
  function svg(value){const selected=Object.hasOwn(drawings,value)?value:'speaker';return '<svg viewBox="0 0 84 64" aria-hidden="true">'+drawings[selected]+'</svg>';}
  const api=Object.freeze({options,kinds:Object.freeze(options.map(row=>row[0])),labels,kind,svg});
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else scope.AirPlayDeviceIcons=api;
})(typeof window==='undefined'?globalThis:window);
