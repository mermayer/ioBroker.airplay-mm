(() => {
  'use strict';
  const $=id=>document.getElementById(id),model=window.AirPlayConfig;
  let config={},draft={},baseline={},change=()=>{},saving=false,dirty=false,effectiveApplied=false,snapshot=null;
  let resolveLoaded,rejectLoaded;
  const loaded=new Promise((resolve,reject)=>{resolveLoaded=resolve;rejectLoaded=reject;});
  loaded.catch(()=>{});
  const timeout=setTimeout(()=>rejectLoaded(new Error('The ioBroker interface could not be loaded. Open it through the instance settings.')),20000);
  function namespace(){const value=`${window.adapter||'airplay-mm'}.${window.instance??0}`;if(!/^airplay-mm\.\d+$/.test(value))throw new Error('Invalid adapter instance.');return value;}
  function instance(){return Number(namespace().split('.')[1]);}
  function notice(text,tone='error'){$('config-notice').textContent=text||'';$('config-notice').hidden=!text;$('config-notice').dataset.tone=tone;}
  function changed(){dirty=true;change(true);$('settings-save').disabled=saving||!!snapshot?.busy||!!snapshot?.queued;notice('');}
  function socketCall(event,...args){return new Promise((resolve,reject)=>{
    if(!window.socket||window.socket.connected===false){reject(new Error('Connection to ioBroker lost.'));return;}
    const timer=setTimeout(()=>reject(new Error('No response from ioBroker.')),8000);
    window.socket.emit(event,...args,(error,value)=>{clearTimeout(timer);error?reject(new Error(String(error))):resolve(value);});
  });}
  function rpc(command,data={}){return new Promise((resolve,reject)=>{
    if(!window.sendTo||window.socket?.connected===false){reject(new Error('Connection to ioBroker lost.'));return;}
    const timer=setTimeout(()=>reject(new Error('No adapter response. A command will not be repeated automatically.')),command==='precache'?35000:command==='discover'?22000:12000);
    window.sendTo(namespace(),command,data,reply=>{clearTimeout(timer);reply?.ok?resolve(reply.result):reject(new Error(reply?.error||'Adapter request failed.'));});
  });}
  async function status(){
    const alive=await socketCall('getState','system.adapter.'+namespace()+'.alive');
    if(alive?.val)return rpc('adminStatus');
    const states=await socketCall('getStates',namespace()+'.*');
    const value=key=>states?.[namespace()+'.'+key]?.val;
    const json=key=>{try{return JSON.parse(value(key)||'[]');}catch{return [];}};
    return {ready:false,devices:json('info.devices'),history:json('info.history'),settings:{target:value('target')||'',volume:value('volume')??15},
      setupStatus:'Adapter is disabled',lastError:'',maxVolume:config.maxVolume||40,canRetry:false,queued:0};
  }
  window.airplayTransport={
    request:async(path,data)=>{await loaded;const actions={status:'adminStatus',speak:'speak',discover:'discover',cancel:'cancel',settings:'settings',retrySetup:'retrySetup',precache:'precache',pairStart:'pairStart',pairFinish:'pairFinish',pairCancel:'pairCancel',forgetPairing:'forgetPairing',disconnect:'disconnect',cacheList:'cacheList',cachePlay:'cachePlay',cacheDelete:'cacheDelete',retryLearning:'retryLearning'};
      if(!actions[path])throw new Error('Unknown interface action.');
      if(path==='status')return {ok:true,...await status()};
      return {ok:true,result:await rpc(actions[path],data||{})};},
    onStatus:state=>{
      snapshot=state;
      if($('speechify-key-status'))$('speechify-key-status').textContent=(state.speechifyKeyPresent?'API key is stored.':'No API key stored yet.')+(state.speechify?.lastError?' '+state.speechify.lastError:'');
      if(!effectiveApplied&&!dirty&&state.effectiveConfig){draft=model.initial(config,state.effectiveConfig);render();effectiveApplied=true;}
      const busy=!!state.busy||!!state.queued;
      $('settings-save').disabled=!dirty||saving||busy;
      $('settings-running').hidden=!busy;
    },
  };
  function addField(parent,label,value,type='text',options){
    const box=document.createElement('label');box.className='device-field';
    const text=document.createElement('span');text.textContent=label;box.append(text);
    const input=document.createElement(options?'select':'input');if(!options)input.type=type;input.setAttribute('aria-label',label);
    if(options)input.className='browser-default';
    if(options)for(const [val,name]of options){const option=document.createElement('option');option.value=val;option.textContent=name;input.append(option);}
    input.value=value;box.append(input);parent.append(box);return input;
  }
  const modeOptions=[['native-ntp','pyatv · Persistent connection (NTP)'],['native','pyatv · Persistent connection (automatic)'],['auto','pyatv · File, automatic'],['1','pyatv · File, AirPlay 1'],['2','pyatv · File, AirPlay 2']];
  function renderDevices(){
    const container=$('settings-devices');container.replaceChildren();
    draft.devices.forEach((row,index)=>{
      const group=document.createElement('fieldset');group.className='settings-device';
      const legend=document.createElement('legend');legend.textContent='Receiver '+(index+1);group.append(legend);
      for(const [key,label,options]of [['name','Name'],['address','IPv4 address'],['kind','Icon',window.AirPlayDeviceIcons.options],['mode','Transport mode',modeOptions]]){
        const input=addField(group,label,row[key]||'','text',options);input.addEventListener('input',()=>{row[key]=input.value;changed();});
      }
      const remove=document.createElement('button');remove.type='button';remove.className='secondary compact';remove.textContent='Remove';remove.setAttribute('aria-label',(row.name||'Receiver '+(index+1))+' remove');
      remove.addEventListener('click',()=>{draft.devices.splice(index,1);changed();renderDevices();});group.append(remove);container.append(group);
    });
    $('settings-add').disabled=draft.devices.length>=32;
  }
  function render(){
    for(const key of ['bind','port','network','defaultMode','maxVolume','speechProvider','piperVoice','airplayPreRollMs','keepAliveSeconds'])$('setting-'+key).value=draft[key]??'';
    $('setting-keepAliveEnabled').checked=draft.keepAliveEnabled;
    $('setting-keepAliveSeconds').disabled=!draft.keepAliveEnabled;
    $('setting-airplayPreRollEnabled').checked=draft.airplayPreRollEnabled;
    $('setting-airplayPreRollMs').disabled=!draft.airplayPreRollEnabled;
    $('piper-voice-field').hidden=draft.speechProvider!=='piper';
    $('setting-seedHosts').value=draft.seedHosts.join('\n');
    $('setting-sayitInstances').value=draft.sayitInstances.join(', ');
    $('setting-alexaLearningEnabled').checked=draft.alexaLearningEnabled;
    $('setting-alexaInstances').value=draft.alexaInstances.join(', ');
    for(const key of ['importPrototype','legacyBridge'])$('setting-'+key).checked=draft[key];
    renderDevices();
  }
  function collect(){
    const next=JSON.parse(JSON.stringify(draft));
    for(const key of ['bind','network','defaultMode','speechProvider','piperVoice'])next[key]=$('setting-'+key).value.trim();
    for(const key of ['port','maxVolume','airplayPreRollMs','keepAliveSeconds'])next[key]=$('setting-'+key).value.trim()===''?NaN:Number($('setting-'+key).value);
    next.keepAliveEnabled=$('setting-keepAliveEnabled').checked;
    next.airplayPreRollEnabled=$('setting-airplayPreRollEnabled').checked;
    next.seedHosts=$('setting-seedHosts').value.split(/[\s,;]+/).filter(Boolean);
    next.sayitInstances=$('setting-sayitInstances').value.split(/[\s,;]+/).filter(Boolean);
    next.alexaLearningEnabled=$('setting-alexaLearningEnabled').checked;
    next.alexaInstances=$('setting-alexaInstances').value.split(/[\s,;]+/).filter(Boolean);
    for(const key of ['importPrototype','legacyBridge'])next[key]=$('setting-'+key).checked;
    return model.validate(next,instance());
  }
  function tab(name){
    for(const key of ['speech','settings']){const selected=key===name;$('tab-'+key).setAttribute('aria-selected',String(selected));$('panel-'+key).hidden=!selected;}
  }
  $('tab-speech').addEventListener('click',()=>tab('speech'));
  $('tab-settings').addEventListener('click',()=>tab('settings'));
  $('panel-settings').addEventListener('input',event=>{if(event.target.id!=='speechify-api-key'&&event.target.matches('input,select,textarea'))changed();});
  $('panel-settings').addEventListener('change',event=>{if(event.target.id!=='speechify-api-key'&&event.target.matches('input,select,textarea'))changed();});
  async function saveSpeechifyKey(key){
    try{await loaded;await rpc('saveSpeechifyKey',{key});$('speechify-api-key').value='';notice(key?'API key stored with encryption.':'API key removed.','success');}
    catch(error){notice(error.message);}
  }
  $('speechify-save-key').addEventListener('click',()=>{const key=$('speechify-api-key').value.trim();if(!key){notice('Enter the API key.');return;}saveSpeechifyKey(key);});
  $('speechify-remove-key').addEventListener('click',()=>saveSpeechifyKey(''));
  $('setting-speechProvider').addEventListener('change',()=>{$('piper-voice-field').hidden=$('setting-speechProvider').value!=='piper';});
  $('setting-airplayPreRollEnabled').addEventListener('change',()=>{$('setting-airplayPreRollMs').disabled=!$('setting-airplayPreRollEnabled').checked;});
  $('setting-keepAliveEnabled').addEventListener('change',()=>{$('setting-keepAliveSeconds').disabled=!$('setting-keepAliveEnabled').checked;});
  $('settings-add').addEventListener('click',()=>{draft.devices.push({name:'',address:'',kind:'speaker',mode:draft.defaultMode});changed();renderDevices();});
  $('settings-reset').addEventListener('click',()=>{draft=model.initial(config,snapshot?.effectiveConfig||{});dirty=false;change(false);render();$('settings-save').disabled=true;notice('Changes discarded.','success');});
  window.load=(settings,onChange)=>{
    clearTimeout(timeout);config=JSON.parse(JSON.stringify(settings||{}));baseline=JSON.parse(JSON.stringify(config));change=onChange;
    draft=model.initial(config);render();change(false);resolveLoaded();
  };
  window.save=callback=>{
    try{if(snapshot?.busy||snapshot?.queued)throw new Error('Wait for active announcements before saving.');callback({...config,...collect()});}
    catch(error){notice(error.message);}
  };
  $('settings-save').addEventListener('click',async()=>{
    if(saving)return;
    try{
      if(snapshot?.busy||snapshot?.queued)throw new Error('Wait for active announcements before saving.');
      const next=collect();saving=true;$('settings-save').disabled=true;notice('');
      const object=await socketCall('getObject','system.adapter.'+namespace());
      if(!object?.native)throw new Error('Adapter settings could not be loaded.');
      const merged=model.merge(object.native,baseline,next);
      if(snapshot?.ready)await rpc('validateConfig',merged);
      await socketCall('setObject','system.adapter.'+namespace(),{...object,native:merged});
      config=merged;baseline=JSON.parse(JSON.stringify(config));draft=model.initial(config);dirty=false;change(false);
      notice('Saved. The adapter applies the settings and restarts if needed.','success');
    }catch(error){notice(error.message);}finally{saving=false;$('settings-save').disabled=!dirty;}
  });
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  function script(src){return new Promise((resolve,reject)=>{const node=document.createElement('script');node.src=src;node.onload=resolve;node.onerror=()=>reject(new Error('ioBroker library could not be loaded.'));document.head.append(node);});}
  async function connect(){
    window.systemDictionary={};
    for(const url of ['../../lib/js/jquery-3.2.1.min.js','../../socket.io/socket.io.js','../../js/translate.js','../../lib/js/materialize.js','../../js/adapter-settings.js'])await script(url);
  }
  connect().catch(error=>{clearTimeout(timeout);rejectLoaded(error);$('status').textContent=error.message;});
})();
