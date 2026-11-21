import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {FakeAdapter,FakeSupervisor,interfaces,until,pause} from './helpers.mjs';
const require=createRequire(import.meta.url);const {Controller,LEGACY}=require('../lib/controller');
function fixture(config={}) {
    const a=new FakeAdapter({legacyBridge:false,...config});const calls=[];
    const runtime={assertDirectory:async x=>x,loadImported:async()=>({}),atomicJSON:async(file,data)=>calls.push({file,data}),prepare:async(root,...args)=>{calls.push({prepare:root});return {installed:true,python:root+'/venv/bin/python',pythonVersion:'3.11.2'};}};
    const c=new Controller(a,{getAbsoluteInstanceDataDir:()=>'/test/airplay-mm.0',getAbsoluteDefaultDataDir:()=>'/test'},
        {interfaces,runtime,Supervisor:FakeSupervisor});return {a,c,calls};
}
async function command(c,key,val){c.stateChange('airplay-mm.0.'+key,{val,ack:false});await c.commandChain;}

test('multi-receiver datapoint selection is acknowledged and reused by old say input',async()=>{
    const {a,c}=fixture();await c.start();await command(c,'targets','["receiver-lg","receiver-other"]');await command(c,'say','Beide Räume');
    assert.deepEqual(c.worker.requests.at(-1).data.targets,['receiver-lg','receiver-other']);assert.equal(c.worker.requests.at(-1).data.target,undefined);
    assert.equal(a.states.get('target').val,'receiver-lg');assert.deepEqual(JSON.parse(a.states.get('targets').val),['receiver-lg','receiver-other']);
    await command(c,'target','receiver-other');await command(c,'say','Ein Raum');assert.equal(c.worker.requests.at(-1).data.target,'receiver-other');assert.equal(c.worker.requests.at(-1).data.targets,undefined);await c.stop();
});

test('explicit single or multiple targets override a saved group',async()=>{
    const {c}=fixture();await c.start();c.values.targets=['receiver-lg','receiver-other'];
    await c.speak({text:'Einzeln',target:'receiver-tv'});assert.equal(c.worker.requests.at(-1).data.targets,undefined);
    await c.speak({text:'Gruppe',targets:['receiver-other','receiver-tv']});assert.equal(c.worker.requests.at(-1).data.target,undefined);assert.deepEqual(c.worker.requests.at(-1).data.targets,['receiver-other','receiver-tv']);await c.stop();
});

test('invalid selection JSON is rejected and a later command remains usable',async()=>{
    const {a,c}=fixture();await c.start();await command(c,'targets','not-json');assert.equal(c.worker.requests.length,0);assert.match(a.states.get('info.lastError').val,/JSON/);await command(c,'say','Danach');assert.equal(c.worker.requests.length,1);await c.stop();
});

test('pairing is available to Admin only; PIN does not enter adapter logs or states',async()=>{
    const {a,c}=fixture();await c.start();
    c.message({from:'system.adapter.javascript.0',command:'pairFinish',message:{target:'receiver-lg',pin:'1234'},callback:{id:21}});await c.commandChain;await pause(0);assert.equal(a.messages.at(-1).value.ok,false);assert.equal(c.worker.requests.length,0);
    c.message({from:'system.adapter.admin.0',command:'pairFinish',message:{target:'receiver-lg',pin:'1234'},callback:{id:22}});await c.commandChain;await pause(0);assert.equal(a.messages.at(-1).value.ok,true);assert.equal(c.worker.requests.at(-1).action,'pairFinish');assert.ok(!JSON.stringify([...a.states,...a.logs]).includes('1234'));await c.stop();
});
test('startup defines only own objects and never replays retained commands',async()=>{
    const {a,c}=fixture();for(const key of ['speak','say','cancel','discover'])a.states.set(key,{val:key==='say'?'Alte Message':true,ack:false});
    await c.start();assert.equal(c.worker.requests.length,0);assert.equal(a.states.get('speak').val,false);assert.equal(a.states.get('say').val,'');assert.equal(a.foreignWrites.length,0);assert.ok(a.objects.size>15);await c.stop();
});
test('an old enabled script is never queried and cannot block a free adapter port',async()=>{
    const {a,c}=fixture({legacyBridge:true});const old='script.js.common.AirPlay_mm';a.objects.set(old,{type:'script',common:{enabled:true}});
    const read=a.getForeignObjectAsync.bind(a);a.getForeignObjectAsync=async id=>{assert.notEqual(id,old);return read(id);};await c.start();
    assert.equal(c.worker.ready,true);assert.equal(a.objects.get(old).common.enabled,true);await c.stop();
});
test('an acknowledged state or an unrelated device cannot trigger speech',async()=>{
    const {c}=fixture();await c.start();for(const id of ['other-adapter.0.say','hm-rpc.0.device.STATE','airplay-mm.1.say','0_userdata.0.AirPlayMM.say'])c.stateChange(id,{val:'Hallo',ack:false});
    c.stateChange('airplay-mm.0.say',{val:'Hallo',ack:true});await c.commandChain;assert.equal(c.worker.requests.length,0);await c.stop();
});
test('sequential datapoint settings precede exactly one selected-target announcement',async()=>{
    const {c}=fixture();await c.start();c.stateChange('airplay-mm.0.target',{val:'receiver-lg',ack:false});c.stateChange('airplay-mm.0.volume',{val:17,ack:false});c.stateChange('airplay-mm.0.say',{val:'Die Waschmaschine ist fertig.',ack:false});await c.commandChain;
    assert.deepEqual(c.worker.requests.map(x=>x.action),['settings','settings','speak']);assert.equal(c.worker.requests[2].data.target,'receiver-lg');assert.equal(c.worker.requests[2].data.volume,17);assert.match(c.worker.requests[2].data.requestId,/^[0-9a-f-]{36}$/);await c.stop();
});
test('draft text and start button issue one announcement and reset the button',async()=>{
    const {a,c}=fixture();await c.start();await command(c,'text','Guten Morgen');assert.equal(c.worker.requests.length,0);await command(c,'speak',true);assert.equal(c.worker.requests[0].data.text,'Guten Morgen');assert.equal(a.states.get('speak').val,false);await c.stop();
});
test('invalid text fails before worker IPC and a later valid command still succeeds',async()=>{
    const {a,c}=fixture();await c.start();await command(c,'say','x'.repeat(501));assert.equal(c.worker.requests.length,0);assert.match(a.states.get('info.lastError').val,/500/);await command(c,'say','Hallo');assert.equal(c.worker.requests.length,1);await c.stop();
});
test('sendTo accepts explicit target and request ID without using foreign devices',async()=>{
    const {a,c}=fixture();await c.start();c.message({from:'system.adapter.javascript.0',command:'speak',message:{text:'Hallo',target:'receiver-other',volume:9,requestId:'unique1'},callback:{id:1}});await c.commandChain;await pause(0);
    assert.equal(c.worker.requests[0].data.target,'receiver-other');assert.equal(c.worker.requests[0].data.requestId,'unique1');assert.equal(a.messages[0].value.ok,true);await c.stop();
});
test('unsupported sendTo payload receives an error and cannot play an external URL',async()=>{
    const {a,c}=fixture();await c.start();c.message({from:'system.adapter.javascript.0',command:'speak',message:{text:'Hallo',url:'http://example.org/audio'},callback:{id:2}});await c.commandChain;await pause(0);assert.equal(a.messages[0].value.ok,false);assert.equal(c.worker.requests.length,0);await c.stop();
});
test('legacy bridge only acknowledges pre-existing selected prototype states',async()=>{
    const {a,c}=fixture({legacyBridge:true});for(const key of ['say','target','volume','info.status'])a.objects.set(LEGACY+key,{type:'state'});await c.start();
    c.stateChange(LEGACY+'say',{val:'Hallo',ack:false});await c.commandChain;assert.equal(c.worker.requests.length,1);assert.ok(a.foreignWrites.every(x=>x.id.startsWith(LEGACY)&&x.ack===true));assert.ok(!a.foreignWrites.some(x=>x.id.includes('homekit')));await c.stop();
});

test('raising the limit repairs old prototype metadata before mirroring 55 or 80 percent',async()=>{
    const {a,c}=fixture({legacyBridge:true,maxVolume:80});
    const acl={owner:'system.user.example',ownerGroup:'system.group.administrator',object:1636,state:1636};
    const native={kept:'existing'};
    a.objects.set(LEGACY+'volume',{type:'state',common:{name:'Announcement volume (0 to 40 percent)',type:'number',role:'level.volume',min:0,max:40,unit:'%',read:true,write:true},acl,native});
    a.states.set('volume',{val:55,ack:true});a.states.set(LEGACY+'volume',{val:55,ack:true});
    const write=a.setForeignStateAsync.bind(a);let mirrored=0;
    a.setForeignStateAsync=async(id,state)=>{
        if(id===LEGACY+'volume'){assert(state.val<=a.objects.get(id).common.max,'Reproduces ioBroker range warning');mirrored++;}
        return write(id,state);
    };
    await c.start();
    assert.equal(c.worker.requests.length,0);assert.equal(a.states.get('volume').val,55);assert.equal(a.states.get(LEGACY+'volume').val,55);
    assert.equal(a.objects.get('volume').common.max,80);assert.equal(a.objects.get(LEGACY+'volume').common.max,80);
    assert.equal(a.objects.get(LEGACY+'volume').common.name,'Announcement volume');
    assert.deepEqual(a.objects.get(LEGACY+'volume').acl,acl);assert.deepEqual(a.objects.get(LEGACY+'volume').native,native);
    for(const volume of [55,80])await c.update({ready:true,devices:[],history:[],settings:{target:'receiver-lg',volume},queued:0});
    assert.equal(mirrored,2);assert.equal(c.worker.requests.length,0);assert.equal(a.logs.filter(x=>x.level==='warn').length,0);await c.stop();
});

test('lowering the configured limit updates both metadata ranges and preserves a custom name',async()=>{
    const {a,c}=fixture({legacyBridge:true,maxVolume:25});
    a.objects.set(LEGACY+'volume',{type:'state',common:{name:{de:'Meine Lautstärke',en:'My volume'},max:80}});
    await c.start();assert.equal(a.objects.get('volume').common.max,25);assert.equal(a.objects.get(LEGACY+'volume').common.max,25);
    assert.deepEqual(a.objects.get(LEGACY+'volume').common.name,{de:'Meine Lautstärke',en:'My volume'});await c.stop();
});

test('disabled or missing compatibility volume is never created or modified',async()=>{
    for(const disabled of [true,false]) {
        const {a,c}=fixture({legacyBridge:!disabled,maxVolume:80});
        const existing={type:'state',common:{name:'Privat',max:40}};
        if(disabled)a.objects.set(LEGACY+'volume',structuredClone(existing));
        await c.start();assert.equal(a.foreignObjectEdits.length,0);
        if(disabled)assert.deepEqual(a.objects.get(LEGACY+'volume'),existing);else assert.equal(a.objects.has(LEGACY+'volume'),false);
        await c.stop();
    }
});

test('a compatibility metadata permission failure leaves the normal speaker usable without repeated mirror errors',async()=>{
    const {a,c}=fixture({legacyBridge:true,maxVolume:80});a.objects.set(LEGACY+'volume',{type:'state',common:{max:40}});
    a.extendForeignObjectAsync=async()=>{throw new Error('permissionError');};
    await c.start();assert.equal(c.worker.ready,true);assert.equal(a.logs.filter(x=>x.level==='warn'&&/permissionError/.test(x.message)).length,1);
    for(const volume of [55,80])await c.update({ready:true,devices:[],history:[],settings:{target:'receiver-lg',volume},queued:0});
    assert.equal(a.states.get('volume').val,80);assert(!a.foreignWrites.some(x=>x.id===LEGACY+'volume'));
    await command(c,'say','Danach weiterhin bedienbar');assert.equal(c.worker.requests.length,1);
    assert.equal(a.logs.filter(x=>x.level==='warn').length,1);await c.stop();
});
test('readiness loss and repeated stop are harmless and stop the worker once',async()=>{
    const {a,c}=fixture();await c.start();c.worker.ready=false;c.worker.emit('offline',{});await pause(0);assert.equal(a.states.get('info.connection').val,false);await Promise.all([c.stop(),c.stop(),c.stop()]);assert.equal(c.worker.stops,1);assert.equal(a.states.get('info.status').val,'Beendet');
});
test('worker history/settings updates do not emit commands',async()=>{
    const {a,c}=fixture();await c.start();await c.update({ready:true,devices:[],history:[{id:'old',status:'completed'}],settings:{target:'receiver-lg',volume:19},queued:0});assert.equal(c.worker.requests.length,0);assert.equal(a.states.get('volume').val,19);assert.equal(JSON.parse(a.states.get('info.history').val)[0].id,'old');await c.stop();
});
test('a failed setup leaves the adapter responsive to retry without a global restart',async()=>{
    const {a,c}=fixture();let failures=1;c.runtime.prepare=async root=>{if(failures--)throw new Error('Netzwerk offline');return {installed:true,python:root+'/venv/bin/python'};};await c.start();assert.equal(c.worker,undefined);assert.match(a.states.get('info.lastError').val,/offline/);await command(c,'retrySetup',true);assert.equal(c.worker.ready,true);await c.stop();
});
test('stopping while startup awaits a filesystem operation cannot start an orphan service',async()=>{
    const {c,calls}=fixture();let release;const blocked=new Promise(resolve=>{release=resolve;});c.runtime.assertDirectory=async x=>{await blocked;return x;};
    const starting=c.start();await until(()=>!!c.abort);await c.stop();release();await starting;assert.equal(c.worker,undefined);assert.ok(!calls.some(x=>x.prepare));
});
test('an invalid command remains visible across heartbeats until a subsequent command succeeds',async()=>{
    const {a,c}=fixture();await c.start();await command(c,'say','x'.repeat(501));
    const state={ready:true,devices:[],history:[],settings:{target:'receiver-lg',volume:15},lastError:''};await c.update(state);assert.match(a.states.get('info.lastError').val,/500/);
    await command(c,'say','Hallo');await c.update(state);assert.equal(a.states.get('info.lastError').val,'');await c.stop();
});
test('admin status remains responsive without entering the command queue or emitting speech',async()=>{
    const {a,c}=fixture();await c.start();await c.update({ready:true,settings:{target:'receiver-lg',volume:15},history:[],devices:[]});
    c.commandCount=16;c.message({from:'system.adapter.admin.0',command:'adminStatus',callback:{id:1}});assert.equal(a.messages.at(-1).value.ok,true);assert.equal(a.messages.at(-1).value.result.ready,true);assert.equal(c.worker.requests.length,0);c.commandCount=0;await c.stop();
});
test('admin selection and volume persist through settings commands without a restart or an announcement',async()=>{
    const {a,c}=fixture();await c.start();const worker=c.worker;c.message({from:'system.adapter.admin.0',command:'settings',message:{target:'receiver-other',volume:21},callback:{id:2}});await c.commandChain;await pause(0);assert.equal(c.worker,worker);assert.equal(worker.requests.length,1);assert.equal(worker.requests[0].action,'settings');assert.equal(a.states.get('target').val,'receiver-other');assert.equal(a.states.get('volume').val,21);assert.equal(a.messages.at(-1).value.ok,true);await c.stop();
});

test('the master datapoint persists its own signal level without changing receiver volume or speaking',async()=>{
    const {a,c}=fixture({maxVolume:80});await c.start();await command(c,'volume',35);c.worker.requests=[];
    const worker=c.worker;await command(c,'masterGain',50);
    assert.deepEqual(worker.requests,[{action:'settings',data:{masterGain:50}}]);assert.equal(c.worker,worker);
    assert.equal(a.states.get('volume').val,35);assert.equal(a.states.get('masterGain').val,50);
    assert.equal(a.objects.get('masterGain').common.min,0);assert.equal(a.objects.get('masterGain').common.max,100);
    assert.equal(a.foreignWrites.length,0);await c.stop();
});

test('master settings from Admin stay separate from explicit per-announcement receiver levels',async()=>{
    const {a,c}=fixture({maxVolume:80});await c.start();
    c.message({from:'system.adapter.admin.0',command:'settings',message:{masterGain:40},callback:{id:99}});await c.commandChain;await pause(0);
    assert.equal(a.messages.at(-1).value.ok,true);assert.equal(a.states.get('masterGain').val,40);
    await c.speak({text:'Signalpegel bleibt zentral',volume:60});
    assert.equal(c.worker.requests.at(-1).data.volume,60);assert.equal(c.worker.requests.at(-1).data.masterGain,undefined);
    assert.equal(c.values.masterGain,40);await c.stop();
});
test('retry while a worker is starting never creates another worker',async()=>{const {c}=fixture();await c.start();const worker=c.worker;worker.ready=false;await c.setup();assert.equal(c.worker,worker);await c.stop();});

function sayitFixture(enabled=false){
    const item=fixture({sayitInstances:['sayit.0']});
    item.a.objects.set('system.adapter.sayit.0',{common:{enabled}});
    for(const key of ['text','cachetext','volume'])item.a.objects.set('sayit.0.tts.'+key,{type:'state'});
    item.a.states.set('sayit.0.tts.text',{val:'Alte Ansage',ack:false,ts:1});
    item.a.states.set('sayit.0.tts.volume',{val:23,ack:true});return item;
}
test('optional SayIt input subscribes only to an installed disabled instance without replay',async()=>{
    const {a,c}=sayitFixture();await c.start();assert.equal(c.worker.requests.length,0);
    assert.ok(a.subscriptions.some(x=>x.foreign==='sayit.0.tts.text'));
    assert.ok(!a.subscriptions.some(x=>x.foreign==='sayit.0.tts.volume'));
    c.stateChange('sayit.0.tts.text',{val:'Veraltet',ack:false,ts:1});
    c.stateChange('sayit.0.tts.text',{val:'Quittiert',ack:true});await c.commandChain;
    assert.equal(c.worker.requests.length,0);assert.equal(a.foreignWrites.length,0);await c.stop();
});
test('new SayIt text uses separate volume and the selected AirPlay target exactly once',async()=>{
    const {a,c}=sayitFixture();await c.start();await command(c,'target','receiver-lg');c.worker.requests=[];
    c.stateChange('sayit.0.tts.text',{val:'Die Waschmaschine ist fertig.',ack:false,ts:Date.now()});await c.commandChain;
    assert.equal(c.worker.requests.length,1);assert.equal(c.worker.requests[0].action,'speak');
    assert.equal(c.worker.requests[0].data.volume,23);assert.equal(c.worker.requests[0].data.target,'receiver-lg');assert.equal(a.foreignWrites.length,0);await c.stop();
});
test('SayIt cachetext only prepares a text; inline volume is bounded on later speech',async()=>{
    const {c}=sayitFixture();await c.start();
    c.stateChange('sayit.0.tts.cachetext',{val:'de;90;Hallo Küche',ack:false});await c.commandChain;
    assert.deepEqual(c.worker.requests.map(x=>x.action),['precache']);assert.deepEqual(c.worker.requests[0].data,{text:'Hallo Küche'});
    c.stateChange('sayit.0.tts.text',{val:'de;90;Hallo Küche',ack:false});await c.commandChain;
    assert.equal(c.worker.requests[1].data.volume,40);await c.stop();
});
test('enabled or missing SayIt cannot cause duplicate audio and never changes the foreign instance',async()=>{
    const {a,c}=sayitFixture(true);await c.start();assert.equal(c.sayitSources.size,0);
    c.stateChange('sayit.0.tts.text',{val:'Nicht sprechen',ack:false});await c.commandChain;
    assert.equal(c.worker.requests.length,0);assert.equal(a.objects.get('system.adapter.sayit.0').common.enabled,true);await c.stop();
    const other=fixture({sayitInstances:['sayit.3']});await other.c.start();assert.equal(other.c.sayitSources.size,0);assert.equal(other.c.worker.ready,true);await other.c.stop();
});
test('SayIt enabled after startup is detected before any speech; invalid text does not poison later commands',async()=>{
    const {a,c}=sayitFixture();await c.start();a.objects.get('system.adapter.sayit.0').common.enabled=true;
    c.stateChange('sayit.0.tts.text',{val:'Nicht doppelt sprechen',ack:false});await c.commandChain;assert.equal(c.worker.requests.length,0);
    a.objects.get('system.adapter.sayit.0').common.enabled=false;
    c.stateChange('sayit.0.tts.text',{val:'http://foreign.invalid/a.mp3',ack:false});await c.commandChain;assert.equal(c.worker.requests.length,0);
    c.stateChange('sayit.0.tts.text',{val:'Jetzt gültig',ack:false});await c.commandChain;assert.equal(c.worker.requests.length,1);await c.stop();
});
test('preparing a text does not block status or cancellation and duplicate preparation is rejected',async()=>{
    const {a,c}=fixture();await c.start();let release;const original=c.worker.request.bind(c.worker);
    c.worker.request=async(action,data)=>action==='precache'?new Promise(resolve=>{release=resolve;}):original(action,data);
    c.message({from:'system.adapter.admin.0',command:'precache',message:{text:'Hallo'},callback:{id:1}});await until(()=>!!release);
    c.message({from:'system.adapter.admin.0',command:'adminStatus',callback:{id:2}});
    assert.equal(a.messages.at(-1).value.ok,true);await assert.rejects(c.prepareText({text:'Doppelt'}),/already/);
    c.message({from:'system.adapter.admin.0',command:'cancel',callback:{id:3}});await c.commandChain;assert.equal(c.worker.requests[0].action,'cancel');
    release({prepared:true});await until(()=>!c.preparing);assert.equal(c.worker.requests.filter(x=>x.action==='speak').length,0);await c.stop();
});
