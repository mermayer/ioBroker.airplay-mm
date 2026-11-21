import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),model=require('../admin/config-model.js'),{normalizeConfig}=require('../lib/config');
import {interfaces} from './helpers.mjs';
const effective={host:'10.0.0.210',port:8098,network:'10.0.0.0/24',seedHosts:['10.0.0.200'],presets:[{address:'10.0.0.200',name:'Example Soundbar',kind:'soundbar'}],modes:{'10.0.0.200':'native-ntp'}};
test('admin editor materializes inherited receiver names and preserves their tested mode',()=>{const draft=model.initial({},effective);assert.equal(draft.devices[0].name,'Example Soundbar');assert.equal(draft.devices[0].mode,'native-ntp');assert.equal(draft.bind,effective.host);assert.equal(draft.schemaVersion,2);assert.equal(model.validate(draft).maxVolume,40);});
test('saving an explicit empty receiver list does not silently restore old imported rows',()=>{const draft=model.initial({},effective);draft.devices=[];draft.seedHosts=[];const parsed=normalizeConfig(model.validate(draft),0,{devices:[{address:'10.0.0.200',name:'Old'}],seedHosts:['10.0.0.200']},interfaces);assert.deepEqual(parsed.presets,[]);assert.deepEqual(parsed.seedHosts,[]);});
test('invalid admin values are rejected before any configuration write',()=>{for(const patch of [{port:80},{maxVolume:101},{bind:'8.8.8.8'},{seedHosts:['https://foreign.invalid']},{network:'10.0.0.0/99'}])assert.throws(()=>model.validate({...model.initial({},effective),...patch}));});
test('device names are text values and duplicate addresses are rejected',()=>{const draft=model.initial({},effective);draft.devices[0].name='<script>alert(1)</script>';assert.equal(model.validate(draft).devices[0].name,draft.devices[0].name);draft.devices.push({...draft.devices[0]});assert.throws(()=>model.validate(draft),/distinct/);});
test('config merge preserves unknown native fields and rejects concurrent changes',()=>{const raw={port:8098,otherOption:'keep'},draft=model.initial(raw,effective);const merged=model.merge({...raw,newerOption:42},raw,draft);assert.equal(merged.newerOption,42);assert.equal(merged.otherOption,'keep');assert.throws(()=>model.merge({...raw,port:9000},raw,draft),/changed elsewhere/);});
test('SayIt input is disabled by default and validated consistently in UI and runtime',()=>{
    const draft=model.initial({},effective);assert.deepEqual(draft.sayitInstances,[]);
    for(const sources of [['sayit.0','sayit.1'],[]]){const data=model.validate({...draft,sayitInstances:sources});assert.deepEqual(normalizeConfig(data,0,{},interfaces).sayitInstances,sources);}
    for(const sources of [['javascript.0'],['sayit.0','sayit.0'],'sayit.0',Array.from({length:9},(_,i)=>'sayit.'+i)]){
        assert.throws(()=>model.validate({...draft,sayitInstances:sources}));assert.throws(()=>normalizeConfig({...draft,sayitInstances:sources},0,{},interfaces));
    }
});
test('speech source is explicit, backward compatible and validated on both sides',()=>{
    const draft=model.initial({},effective);assert.equal(draft.speechProvider,'piper');
    const google=model.validate({...draft,speechProvider:'sayit-google'});assert.equal(normalizeConfig(google,0,{},interfaces).speechProvider,'sayit-google');
    for(const source of ['google',{},'https://invalid']){assert.throws(()=>model.validate({...draft,speechProvider:source}));assert.throws(()=>normalizeConfig({...draft,speechProvider:source},0,{},interfaces));}
});
test('local voice defaults to Thorsten and Ramona is preserved through config saves',()=>{
    const draft=model.initial({},effective);assert.equal(draft.piperVoice,'de_DE-thorsten-medium');
    const ramona=model.validate({...draft,piperVoice:'de_DE-ramona-low'});assert.equal(normalizeConfig(ramona,0,{},interfaces).piperVoice,'de_DE-ramona-low');
    assert.equal(model.merge({}, {},ramona).piperVoice,'de_DE-ramona-low');
    for(const voice of ['Ramona','../../ramona','unknown',{}]){assert.throws(()=>model.validate({...draft,piperVoice:voice}));assert.throws(()=>normalizeConfig({...draft,piperVoice:voice},0,{},interfaces));}
});
test('pre-roll can be disabled without losing its duration and rejects invalid values',()=>{
    const draft=model.initial({},effective);assert.equal(draft.airplayPreRollEnabled,true);assert.equal(draft.airplayPreRollMs,700);
    for(const enabled of [true,false])for(const ms of [0,350,2000]){const value=model.validate({...draft,airplayPreRollEnabled:enabled,airplayPreRollMs:ms}),backend=normalizeConfig(value,0,{},interfaces);assert.equal(backend.airplayPreRollEnabled,enabled);assert.equal(backend.airplayPreRollMs,ms);}
    for(const patch of [{airplayPreRollEnabled:'false'},{airplayPreRollEnabled:0},{airplayPreRollMs:-1},{airplayPreRollMs:2001},{airplayPreRollMs:0.5},{airplayPreRollMs:NaN},{airplayPreRollMs:'700'},{airplayPreRollMs:true}]){
        assert.throws(()=>model.validate({...draft,...patch}));assert.throws(()=>normalizeConfig({...draft,...patch},0,{},interfaces));
    }
});
test('admin and direct view share the same controls without a frame or the HTTP endpoint',async()=>{const html=await fs.readFile(new URL('../admin/index_m.html',import.meta.url),'utf8'),js=await fs.readFile(new URL('../admin/admin.js',import.meta.url),'utf8');assert.ok(!/<iframe/i.test(html));assert.ok(!js.includes('8098/api'));assert.match(js,/window\.sendTo/);for(const name of ['tab-speech','tab-settings','devices','message','volume','history','settings-save'])assert.ok(html.includes('id="'+name+'"'));assert.equal(await fs.readFile(new URL('../admin/app.js',import.meta.url),'utf8'),await fs.readFile(new URL('../web/app.js',import.meta.url),'utf8'));});
