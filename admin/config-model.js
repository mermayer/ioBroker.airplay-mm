(function (scope) {
  'use strict';
  const keys = ['bind','port','network','seedHosts','devices','defaultMode','maxVolume','importPrototype','legacyBridge','schemaVersion','sayitInstances','speechProvider','piperVoice','airplayPreRollEnabled','airplayPreRollMs','keepAliveEnabled','keepAliveSeconds','alexaLearningEnabled','alexaInstances'];
  const modes = new Set(['native-ntp','native','auto','1','2']);
  const deviceIcons=typeof module!=='undefined'&&module.exports?require('../web/device-icons.js'):scope.AirPlayDeviceIcons;
  const copy = value => JSON.parse(JSON.stringify(value));
  function initial(native = {}, effective = {}) {
    const prior = native.schemaVersion >= 2;
    return {bind:native.bind || effective.host || '',port:native.port ?? 8098,network:native.network || effective.network || '',
      seedHosts:copy(prior || native.seedHosts?.length ? native.seedHosts || [] : effective.seedHosts || []),
      devices:copy(prior || native.devices?.length ? native.devices || [] : (effective.presets || []).map(row => ({...row,mode:effective.modes?.[row.address] || effective.defaultMode || 'native-ntp'}))),
      defaultMode:native.defaultMode || 'native-ntp',maxVolume:native.maxVolume ?? 40,
      importPrototype:native.importPrototype !== false,legacyBridge:native.legacyBridge !== false,schemaVersion:2,sayitInstances:copy(native.sayitInstances||[]),speechProvider:native.speechProvider??'piper',piperVoice:native.piperVoice??'de_DE-thorsten-medium',airplayPreRollEnabled:native.airplayPreRollEnabled??true,airplayPreRollMs:native.airplayPreRollMs??700,keepAliveEnabled:native.keepAliveEnabled??true,keepAliveSeconds:native.keepAliveSeconds??300,alexaLearningEnabled:native.alexaLearningEnabled??false,alexaInstances:copy(native.alexaInstances??['alexa2.0'])};
  }
  function ip(value) {
    if(typeof value!=='string'||!/^\d{1,3}(\.\d{1,3}){3}$/.test(value))return false;
    const parts=value.split('.').map(Number);if(parts.some(x=>x>255)||parts.some((x,i)=>String(x)!==value.split('.')[i]))return false;
    return parts[0]===10||parts[0]===127||(parts[0]===192&&parts[1]===168)||(parts[0]===172&&parts[1]>=16&&parts[1]<=31);
  }
  function validate(value,instance=0) {
    const draft=copy(value);
    if(draft.bind&&!ip(draft.bind))throw new Error('Enter a local IPv4 address or leave blank for automatic selection.');
    if(!Number.isInteger(draft.port)||draft.port<1024||draft.port>65535)throw new Error('Port must be between 1024 and 65535.');
    if(!Number.isInteger(draft.maxVolume)||draft.maxVolume<1||draft.maxVolume>100)throw new Error('Maximum volume must be between 1 and 100.');
    if(draft.network){const [base,bits,extra]=draft.network.split('/');if(!ip(base)||!/^\d{1,2}$/.test(bits||'')||+bits<8||+bits>32||extra!==undefined)throw new Error('Enter a local discovery network such as 192.168.1.0/24.');}
    if(!Array.isArray(draft.seedHosts)||draft.seedHosts.length>32||draft.seedHosts.some(x=>!ip(x)))throw new Error('Up to 32 local receiver addresses are allowed.');
    draft.seedHosts=[...new Set(draft.seedHosts)];
    if(!modes.has(draft.defaultMode))throw new Error('Unknown transport mode.');
    if(!['piper','sayit-google','speechify'].includes(draft.speechProvider))throw new Error('Unknown speech provider.');
    if(typeof draft.alexaLearningEnabled!=='boolean'||!Array.isArray(draft.alexaInstances)||draft.alexaInstances.length>8||draft.alexaInstances.some(x=>!/^alexa2\.\d+$/.test(x))||new Set(draft.alexaInstances).size!==draft.alexaInstances.length)throw new Error('Enter distinct Alexa instances such as alexa2.0.');
    if(!['de_DE-thorsten-medium','de_DE-ramona-low'].includes(draft.piperVoice))throw new Error('Unknown local voice.');
    if(typeof draft.airplayPreRollEnabled!=='boolean')throw new Error('Pre-roll must be enabled or disabled.');
    if(!Number.isInteger(draft.airplayPreRollMs)||draft.airplayPreRollMs<0||draft.airplayPreRollMs>2000)throw new Error('Pre-roll must be between 0 and 2000 milliseconds.');
    if(typeof draft.keepAliveEnabled!=='boolean'||!Number.isInteger(draft.keepAliveSeconds)||draft.keepAliveSeconds<0||draft.keepAliveSeconds>3600)throw new Error('Persistent connections: choose a duration between 0 and 3600 seconds.');
    if(!Array.isArray(draft.devices)||draft.devices.length>32)throw new Error('Up to 32 preset receivers are allowed.');
    const addresses=new Set();
    for(const row of draft.devices){
      if(!ip(row.address)||addresses.has(row.address))throw new Error('Preset receivers require distinct local IPv4 addresses.');
      if(typeof row.name!=='string'||!row.name.trim()||row.name.length>80||/[\x00-\x1f]/.test(row.name))throw new Error('Enter a name with 1 to 80 characters for each preset receiver.');
      if(!modes.has(row.mode)||!deviceIcons.kinds.includes(row.kind))throw new Error('Unknown receiver type or transport mode.');
      row.name=row.name.trim();addresses.add(row.address);
    }
    if(instance!==0&&draft.legacyBridge)throw new Error('Legacy 0_userdata states can only be used by instance 0.');
    if(!Array.isArray(draft.sayitInstances)||draft.sayitInstances.length>8||draft.sayitInstances.some(x=>!/^sayit\.\d+$/.test(x))||new Set(draft.sayitInstances).size!==draft.sayitInstances.length)throw new Error('Enter distinct SayIt instances such as sayit.0.');
    draft.schemaVersion=2;return draft;
  }
  function merge(current,baseline,draft) {
    for(const key of keys){
      const now=JSON.stringify(current[key]),before=JSON.stringify(baseline[key]),next=JSON.stringify(draft[key]);
      if(now!==before&&now!==next)throw new Error('Settings have changed elsewhere. Reopen the adapter interface.');
    }
    const result=copy(current);for(const key of keys)result[key]=copy(draft[key]);return result;
  }
  const api={keys,initial,validate,merge};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else scope.AirPlayConfig=api;
})(typeof window==='undefined'?globalThis:window);
