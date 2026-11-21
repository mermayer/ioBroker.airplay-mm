import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {FakeAdapter,FakeSupervisor,interfaces,pause,until} from './helpers.mjs';
const require=createRequire(import.meta.url),{alexaSource,alexaText}=require('../lib/alexa-learning'),{Controller}=require('../lib/controller'),secrets=require('../lib/speechify-key');
function fixture(){const a=new FakeAdapter({legacyBridge:false,alexaLearningEnabled:true,alexaInstances:['alexa2.0']});const runtime={assertDirectory:async x=>x,loadImported:async()=>({}),atomicJSON:async()=>{},prepare:async()=>({pythonVersion:'test'})};const keys={readKey:async()=>'',saveKey:async()=>{}};const c=new Controller(a,{getAbsoluteInstanceDataDir:()=>'/test/airplay-mm.0',getAbsoluteDefaultDataDir:()=>'/test'},{interfaces,runtime,Supervisor:FakeSupervisor,secrets:keys});return{a,c};}
test('only configured Alexa speech commands are observed; volume and textcommands are not learned',()=>{
 for(const key of ['speak','announcement','ssml'])assert(alexaSource('alexa2.0.Echo-Devices.DEVICE.Commands.'+key,['alexa2.0']));
 for(const key of ['speak-volume','textcommand','sound'])assert(!alexaSource('alexa2.0.Echo-Devices.DEVICE.Commands.'+key,['alexa2.0']));
 assert(!alexaSource('alexa2.1.Echo-Devices.DEVICE.Commands.speak',['alexa2.0']));
 assert.equal(alexaText('70;Die Tür ist zu;Das Licht ist an'),'Die Tür ist zu Das Licht ist an');
 assert.equal(alexaText('<speak>Die T&#252;r ist <break time="200ms"/>zu.</speak>'),'Die Tür ist zu.');
 assert.equal(alexaText('<audio src="soundbank://beep"/>'),null);assert.throws(()=>alexaText('x'.repeat(501)),/500/);
});
test('learning performs zero Alexa writes and zero playback, including repeated and acknowledged events',async()=>{
 const{a,c}=fixture();await c.start();const id='alexa2.0.Echo-Devices.DEVICE.Commands.speak';
 c.stateChange(id,{val:'Die Tür ist zu',ack:true});c.stateChange(id,{val:'Alt',ack:false,ts:0});
 c.stateChange(id,{val:'70;Die Tür ist zu',ack:false,ts:Date.now()});c.stateChange(id,{val:'70;Die Tür ist zu',ack:false,ts:Date.now()});await until(()=>c.worker.requests.length===1&&c.learningBacklog.length===0);
 assert.equal(c.worker.requests.length,1);assert.deepEqual(c.worker.requests[0],{action:'learn',data:{text:'Die Tür ist zu'}});
 assert.equal(a.foreignWrites.length,0);assert(!a.writes.some(x=>x.id.startsWith('alexa2.')));assert(!c.worker.requests.some(x=>x.action==='speak'));await c.stop();
});
test('background input is buffered while the worker is unavailable and does not poison normal commands',async()=>{
 const{a,c}=fixture();await c.start();c.worker.ready=false;c.stateChange('alexa2.0.Echo-Devices.DEVICE.Commands.announcement',{val:'Klingel',ack:false});assert.equal(c.learningBacklog.length,1);
 c.worker.ready=true;c.flushLearning();await until(()=>c.learningBacklog.length===0);assert.equal(a.foreignWrites.length,0);await c.speak({text:'Manueller Test',target:'receiver-lg'});assert.equal(c.worker.requests.at(-1).action,'speak');await c.stop();
});
test('the API key is encrypted at rest, not returned by adminStatus, and non-admin cannot replace it',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'airplay-key-'));const adapter={encrypt:value=>'encrypted:'+Buffer.from(value).toString('base64'),decrypt:value=>Buffer.from(value.slice(10),'base64').toString()};
 try{const key='test-secret-key-12345';await secrets.saveKey(root,adapter,key);assert(!(await fs.readFile(path.join(root,'speechify-key.json'),'utf8')).includes(key));assert.equal(await secrets.readKey(root,adapter),key);await secrets.saveKey(root,adapter,'');assert.equal(await secrets.readKey(root,adapter),'');}finally{await fs.rm(root,{recursive:true,force:true});}
 const{a,c}=fixture();await c.start();c.message({from:'system.adapter.javascript.0',command:'saveSpeechifyKey',message:{key:'do-not-store'},callback:{id:1}});await c.commandChain;await pause(0);assert.equal(a.messages.at(-1).value.ok,false);
 c.message({from:'system.adapter.admin.0',command:'saveSpeechifyKey',message:{key:'test-secret-key'},callback:{id:2}});await c.commandChain;await pause(0);assert.equal(a.messages.at(-1).value.ok,true);
 c.message({from:'system.adapter.admin.0',command:'adminStatus',callback:{id:3}});assert(!JSON.stringify(a.messages.at(-1)).includes('test-secret-key'));assert(!JSON.stringify(a.logs).includes('test-secret-key'));assert(!JSON.stringify([...a.states]).includes('test-secret-key'));await c.stop();
});
