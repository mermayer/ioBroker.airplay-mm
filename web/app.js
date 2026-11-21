(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const icons = window.AirPlayDeviceIcons;
  const labels = {queued:'Queued',generating:'Generating speech',connecting:'Connecting',playing:'Sending',completed:'Delivered',partial:'Partially delivered',failed:'Failed',cancelled:'Cancelled',interrupted:'Interrupted'};
  let csrf = '', snapshot = null, selected = '', connected = false, submitting = false, initialized = false;
  let deviceSignature = '', historySignature = '', settingsPending = 0, settingsChain = Promise.resolve(), preparing = false;
  let selectedTargets = [], multiple = false, pairSubmitting = false;
  let cacheOffset=0,cacheLoading=false,cacheChangedAt=0,cacheTotal=0;
  const targets = () => multiple ? selectedTargets : selected ? [selected] : [];
  const message = $('message'), volume = $('volume');
  try { const draft = localStorage.getItem('airplay-mm-draft'); if (draft) message.value = draft.slice(0, 500); } catch {}
  function notice(text,tone='error') { $('notice').textContent = text || ''; $('notice').hidden = !text; $('notice').dataset.tone=tone; }
  function counter() { $('counter').textContent = `${Array.from(message.value).length} / 500`; updateButtons(); }
  function volumeLabel() { $('volume-value').textContent = `${volume.value} %`; }
  function gainLabel() { if($('master-gain'))$('master-gain-value').textContent = `${$('master-gain').value} %`; }
  function updateButtons() {
    const device = snapshot?.devices?.find(d => targets().includes(d.id) && d.available);
    const pairing = snapshot?.pairing;
    const pairingBusy = pairing && ['connecting','pin','verifying'].includes(pairing.phase) && targets().includes(pairing.target);
    $('speak').disabled = !connected || !device || pairingBusy || !message.value.trim() || submitting || settingsPending > 0;
    $('cancel').disabled = !connected || (!snapshot?.busy && !snapshot?.queued);
    $('discover').disabled = !connected || !!snapshot?.discovering;
    if($('precache'))$('precache').disabled=!connected||!message.value.trim()||preparing||!!snapshot?.preparingText;
  }
  async function api(path, body) {
    if (window.airplayTransport) return window.airplayTransport.request(path, body);
    const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : {'Content-Type':'application/json','X-AirPlay-CSRF':csrf},
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(path==='precache'?35000:20000), cache:'no-store'});
    let data; try { data = await response.json(); } catch { throw new Error('Local speech output returned an invalid response'); }
    if (!response.ok || !data.ok) throw new Error(data.error || 'Request failed');
    return data;
  }
  function saveSettings(data) {
    settingsPending++; updateButtons();
    const request = settingsChain.then(() => api('settings', data));
    settingsChain = request.catch(error => { notice(error.message); }).finally(async () => { settingsPending--; await refresh(); });
    return settingsChain;
  }
  function element(tag, className, text) { const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = text; return e; }
  function renderDevices() {
    const devices = snapshot?.devices || [];
    const signature = JSON.stringify([devices.map(d => [d.id,d.name,d.address,d.kind,d.model,d.modelIdentifier,d.advertisedName,d.available,d.reason,d.paired,d.pairable]), targets(),multiple,
      snapshot?.connections?.map(c=>[c.target,c.state,c.warm]),snapshot?.pairing?.phase]);
    if (signature === deviceSignature) return;
    deviceSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const device of devices) {
      const button = element('button', 'device');
      button.type = 'button'; button.disabled = !device.available && !(multiple&&selectedTargets.includes(device.id));
      button.setAttribute('aria-pressed', String(targets().includes(device.id)));
      button.setAttribute('aria-label', device.name + ', ' + (device.available ? 'reachable' : device.reason || 'unreachable'));
      const iconKind=icons.kind(device),icon = element('span', 'device-icon');
      icon.innerHTML = icons.svg(iconKind);icon.dataset.kind=iconKind;icon.title=icons.labels[iconKind];
      const copy = element('span', 'device-copy');
      const connection = snapshot?.connections?.find(c=>c.target===device.id);
      copy.append(element('span', 'device-name', device.name), element('span', 'device-address', device.address+(connection?' · Connection ready':device.paired?' · paired':'')));
      const dot = element('span', 'device-dot' + (device.available ? ' available' : ''));
      dot.title = device.available ? 'Discovered on the network' : device.reason || 'Unreachable';
      button.append(icon, copy, dot);
      button.title = device.available ? 'Select this receiver' : device.reason || 'Unreachable';
      button.addEventListener('click', () => {
        if(multiple){
          if(selectedTargets.includes(device.id)){
            if(selectedTargets.length===1){notice('Keep at least one receiver selected.');return;}
            selectedTargets=selectedTargets.filter(id=>id!==device.id);
          }else{
            if(selectedTargets.length>=8){notice('Select up to eight receivers at a time.');return;}
            selectedTargets=[...selectedTargets,device.id];
          }
          selected=selectedTargets[0];notice('');renderDevices();saveSettings({targets:selectedTargets});
        }else{selected=device.id;selectedTargets=[selected];notice('');renderDevices();saveSettings({target:selected});}
      });
      const item=element('div','receiver-item');item.append(button);
      const actions=element('div','receiver-actions');
      function action(text,command){
        const b=element('button','secondary compact',text);b.type='button';b.disabled=!connected||pairSubmitting||(['pairStart','forgetPairing'].includes(command)&&['connecting','pin','verifying'].includes(snapshot?.pairing?.phase));
        b.setAttribute('aria-label',device.name+' '+text.toLowerCase());
        b.addEventListener('click',async()=>{b.disabled=true;notice('');try{await api(command,{target:device.id});}catch(error){notice(error.message);}await refresh();});actions.append(b);
      }
      if(device.pairable&&(!device.available||device.paired))action(device.paired?'Pair again':'Pair','pairStart');
      if(connection)action('Release connection','disconnect');
      if(device.paired)action('Remove pairing','forgetPairing');
      if(actions.children.length)item.append(actions);
      fragment.append(item);
    }
    if (!devices.length) fragment.append(element('p', 'receiver-note', snapshot?.discovering ? 'Searching for receivers …' : 'No receivers found yet. Choose Search.'));
    $('devices').replaceChildren(fragment);
    if($('receiver-note'))$('receiver-note').textContent=multiple?`${targets().length} of up to 8 receivers selected · Results per receiver are shown in history`:'One receiver per announcement';
  }

  function renderPairing(){
    const p=snapshot?.pairing,panel=$('pair-panel');if(!panel)return;
    panel.hidden=!p;if(!p)return;
    $('pair-title').textContent=p.name||'Pair receiver';
    const text={connecting:'Preparing pairing …',pin:'Read the PIN from the receiver and enter it here.',verifying:'Checking PIN …',paired:'Pairing saved. The receiver can now be selected.',failed:'Pairing failed.',cancelled:'Pairing cancelled.'};
    $('pair-status').textContent=p.error||text[p.phase]||p.phase;
    $('pair-status').className=p.phase==='failed'?'notice':'';
    $('pair-pin-field').hidden=p.phase!=='pin';
    $('pair-submit').hidden=p.phase!=='pin';
    $('pair-submit').disabled=pairSubmitting||p.phase!=='pin'||!$('pair-pin').value;
    $('pair-cancel').textContent=['paired','failed','cancelled'].includes(p.phase)?'Close':'Cancel';
    if(p.phase!=='pin')$('pair-pin').value='';
  }
  function renderHistory() {
    const history = (snapshot?.history || []).slice(0, 10);
    const signature = JSON.stringify(history);
    if (signature === historySignature) return;
    historySignature = signature;
    const opened = new Set(Array.from($('history').querySelectorAll('details[open]'), node => node.dataset.jobId));
    const fragment = document.createDocumentFragment();
    for (const job of history) {
      const row = element('tr');
      const date = new Date(job.created * 1000);
      const time = element('td', '', date.toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'}));
      time.title = date.toLocaleString('de-DE');
      const result = element('td', 'result-' + job.status);
      if (job.error || typeof job.generationMs==='number') {
        const details = element('details'), summary = element('summary', '', labels[job.status] || job.status);
        details.dataset.jobId = job.id; details.open = opened.has(job.id);
        details.append(summary);
        if(job.error)details.append(element('p', '', job.error));
        if(job.voice)details.append(element('p','timing','Voice: '+job.voice));
        if(job.fallbackReason)details.append(element('p','result-partial','Ramona fallback: '+job.fallbackReason));
        if(typeof job.generationMs==='number')details.append(element('p','timing',`${job.cacheHit?'From cache':'Speech generated'}: ${Math.round(job.generationMs)} ms${typeof job.connectMs==='number'?` · AirPlay-Aufbau: ${(job.connectMs/1000).toFixed(1)} s`:''}`));
        if(job.reusedConnection===true)details.append(element('p','timing','Existing AirPlay connection reused'));
        for(const target of job.targetResults||[]){
          details.append(element('p','target-result '+(target.status==='failed'?'result-failed':''),`${target.name}: ${labels[target.status]||target.status}${target.reusedConnection?' · connection reused':''}${target.error?' · '+target.error:''}`));
        }
        if(typeof job.receiverBufferMs==='number')details.append(element('p','timing',`Receiver buffer: ${(job.receiverBufferMs/1000).toFixed(2)} s · Pre-roll: ${((job.preRollMs||0)/1000).toFixed(2)} s`));
        if(typeof job.estimatedStartMs==='number')details.append(element('p','timing',`Estimated speech start: ${(job.estimatedStartMs/1000).toFixed(2)} s after accepting the request`));
        result.append(details);
      } else result.textContent = labels[job.status] || job.status;
      row.append(time, element('td', '', job.targetName || job.target), element('td', '', job.text), result);
      fragment.append(row);
    }
    if (!history.length) { const row = element('tr', 'empty'); const cell = element('td', '', 'No announcements yet.'); cell.colSpan = 4; row.append(cell); fragment.append(row); }
    $('history').replaceChildren(fragment);
  }
  async function refresh() {
    try {
      snapshot = await api('status'); csrf = snapshot.csrf; connected = !!snapshot.ready;
      if($('adapter-version')&&snapshot.version)$('adapter-version').textContent=snapshot.version;
      if (!initialized || (!settingsPending && document.activeElement !== volume)) {
        selected=snapshot.settings?.target||'';selectedTargets=snapshot.settings?.targets|| (selected?[selected]:[]);
        if(selectedTargets.length>1)multiple=true;
        if($('multi-targets'))$('multi-targets').checked=multiple;
        volume.max = snapshot.maxVolume || 40; volume.value = snapshot.settings?.volume ?? 15; initialized = true; volumeLabel();
      }
      if($('master-gain')&&!settingsPending&&document.activeElement!==$('master-gain')){
        $('master-gain').value=snapshot.masterGain??snapshot.settings?.masterGain??100;gainLabel();
      }
      const active = snapshot.active;
      $('status').textContent = !connected ? snapshot.setupStatus || 'Speech output unavailable' : active ? labels[active.status] || 'Announcement in progress' : snapshot.discovering ? 'Discovery in progress' : snapshot.lastError ? 'Ready · Last request failed' : 'Ready';
      $('status-dot').className = 'status-dot ' + (!connected ? 'offline' : active || snapshot.discovering ? 'working' : snapshot.lastError ? 'failed' : 'ready');
      if ($('adapter-error')) { $('adapter-error').textContent = snapshot.lastError || ''; $('adapter-error').hidden = !snapshot.lastError; }
      if ($('setup-retry')) { $('setup-retry').hidden = connected || !snapshot.canRetry; $('setup-retry').disabled = !snapshot.canRetry; }
      if (window.airplayTransport?.onStatus) window.airplayTransport.onStatus(snapshot);
      if($('speech-status')){
        const speech=snapshot.speech;
        const source=['sayit-google','speechify'].includes(speech?.provider)?'New texts online · Cached announcements local':speech?.ready?'Voice loaded':'Voice loads when needed';
        $('speech-status').textContent=speech?`${source} · Cache: ${speech.entries} / ${speech.maxEntries} texts${speech.provider==='speechify'&&snapshot.speechify?.lastError?' · Ramona fallback: '+snapshot.speechify.lastError:''}`:'';
        if($('voice-label'))$('voice-label').textContent='Voice: '+(speech?.voice||snapshot.voice||'Thorsten · German · Local');
      }
      if($('learning-status')){
        const learning=snapshot.learning;$('learning-status').hidden=!learning?.enabled;
        if(learning?.enabled){$('learning-summary').textContent=`${learning.completed||0} texts prepared · ${learning.pending||0} pending · ${learning.failed||0} failed${learning.hasKey?'':' · API key missing'}${learning.lastError?' · '+learning.lastError:''}`;$('learning-retry').disabled=!connected||!learning.failed;}
      }
      if($('cache-viewer')?.open&&Date.now()-cacheChangedAt>5000)loadCache();
      renderDevices();renderPairing();renderHistory();updateButtons();
    } catch (error) {
      connected = false; $('status').textContent = 'Disconnected'; $('status-dot').className = 'status-dot offline'; updateButtons();
    }
  }
  message.addEventListener('input', () => { counter(); try { localStorage.setItem('airplay-mm-draft', message.value); } catch {} });
  volume.addEventListener('input', volumeLabel);
  volume.addEventListener('change', () => { saveSettings({volume:Number(volume.value)}); });
  if($('master-gain')){
    $('master-gain').addEventListener('input',gainLabel);
    $('master-gain').addEventListener('change',()=>saveSettings({masterGain:Number($('master-gain').value)}));
  }
  if ($('setup-retry')) $('setup-retry').addEventListener('click', async () => { $('setup-retry').disabled = true; try { await api('retrySetup', {}); } catch (error) { notice(error.message); } await refresh(); });
  $('discover').addEventListener('click', async () => {
    $('discover').disabled = true; notice('');
    try { await api('discover', {}); } catch (error) { notice(error.message); }
    await refresh();
  });
  $('speak').addEventListener('click', async () => {
    submitting = true; updateButtons(); notice('');
    const bytes = new Uint8Array(12); crypto.getRandomValues(bytes);
    const requestId = Date.now().toString(16) + '-' + Array.from(bytes, v => v.toString(16).padStart(2,'0')).join('');
    try { await api('speak', {text:message.value,...(multiple?{targets:targets()}:{target:selected}),volume:Number(volume.value),requestId}); }
    catch (error) { notice(error.message); }
    finally { submitting = false; await refresh(); }
  });
  $('cancel').addEventListener('click', async () => { notice(''); try { await api('cancel', {}); } catch (error) { notice(error.message); } await refresh(); });
  if($('precache'))$('precache').addEventListener('click',async()=>{
    preparing=true;notice('');updateButtons();
    try{const response=await api('precache',{text:message.value});const result=response.result||response;notice(result.fallbackReason?'Text prepared using Ramona fallback. '+result.fallbackReason:'Text prepared; playback will use the cached file.','success');}
    catch(error){notice(error.message);}finally{preparing=false;await refresh();}
  });
  if($('multi-targets'))$('multi-targets').addEventListener('change',()=>{
    multiple=$('multi-targets').checked;
    if(!multiple&&selectedTargets.length){selected=selectedTargets[0];selectedTargets=[selected];saveSettings({target:selected});}
    renderDevices();updateButtons();
  });
  if($('pair-pin'))$('pair-pin').addEventListener('input',renderPairing);
  if($('pair-form'))$('pair-form').addEventListener('submit',async event=>{
    event.preventDefault();if(pairSubmitting||!snapshot?.pairing)return;
    const pin=$('pair-pin').value,target=snapshot.pairing.target;$('pair-pin').value='';pairSubmitting=true;renderPairing();
    try{await api('pairFinish',{target,pin});}catch(error){notice(error.message);}finally{pairSubmitting=false;await refresh();}
  });
  if($('pair-cancel'))$('pair-cancel').addEventListener('click',async()=>{
    if(!snapshot?.pairing)return;$('pair-pin').value='';
    try{await api('pairCancel',{target:snapshot.pairing.target});}catch(error){notice(error.message);}await refresh();
  });
  if($('sample-text'))$('sample-text').addEventListener('click',()=>{message.value='Hello. The light is on. The window was opened. The front door is closed.';message.dispatchEvent(new Event('input'));message.focus();});
  async function loadCache(){
    if(cacheLoading||!connected)return;cacheLoading=true;
    try{
      const response=await api('cacheList',{offset:cacheOffset,limit:50}),data=response.result;
      cacheTotal=data.total;cacheChangedAt=Date.now();$('cache-summary').textContent=`${data.total} entries · ${(data.bytes/1024/1024).toFixed(1)} MB`;
      const fragment=document.createDocumentFragment();
      for(const entry of data.entries){
        const row=element('tr'),actions=element('td','cache-actions');
        const play=element('button','secondary compact','Play');play.type='button';play.setAttribute('aria-label','Play cached text: '+entry.text);play.disabled=!snapshot?.devices?.some(d=>targets().includes(d.id)&&d.available);
        play.addEventListener('click',async()=>{play.disabled=true;try{await api('cachePlay',{key:entry.key,...(multiple?{targets:targets()}:{target:selected}),volume:Number(volume.value),requestId:Date.now().toString(16)+'-'+crypto.getRandomValues(new Uint32Array(2)).join('-')});notice('Cached announcement started.','success');}catch(error){notice(error.message);}finally{await refresh();await loadCache();}});
        const remove=element('button','secondary compact','Delete');remove.type='button';remove.setAttribute('aria-label','Delete cached text: '+entry.text);
        remove.addEventListener('click',async()=>{remove.disabled=true;try{await api('cacheDelete',{key:entry.key});notice('Cache entry deleted.','success');}catch(error){notice(error.message);}finally{await loadCache();}});
        actions.append(play,remove);row.append(element('td','',entry.text),element('td','',entry.voice+(entry.prepared?' · Dauerbestand':'')),element('td','',Math.round(entry.bytes/1024)+' KB'),actions);fragment.append(row);
      }
      if(!data.entries.length){const row=element('tr'),cell=element('td','','No cache entries yet.');cell.colSpan=4;row.append(cell);fragment.append(row);}
      $('cache-entries').replaceChildren(fragment);$('cache-prev').disabled=cacheOffset===0;$('cache-next').disabled=cacheOffset+50>=cacheTotal;
    }catch(error){notice(error.message);}finally{cacheLoading=false;}
  }
  if($('cache-viewer'))$('cache-viewer').addEventListener('toggle',()=>{if($('cache-viewer').open)loadCache();});
  if($('cache-refresh'))$('cache-refresh').addEventListener('click',()=>loadCache());
  if($('cache-prev'))$('cache-prev').addEventListener('click',()=>{cacheOffset=Math.max(0,cacheOffset-50);loadCache();});
  if($('cache-next'))$('cache-next').addEventListener('click',()=>{if(cacheOffset+50<cacheTotal){cacheOffset+=50;loadCache();}});
  if($('learning-retry'))$('learning-retry').addEventListener('click',async()=>{try{await api('retryLearning',{});await refresh();}catch(error){notice(error.message);}});
  async function poll() { await refresh(); setTimeout(poll, document.hidden ? 5000 : 2000); }
  counter(); volumeLabel(); poll();
})();
