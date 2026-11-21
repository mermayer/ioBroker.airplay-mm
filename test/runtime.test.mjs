import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import net from 'node:net';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
const require=createRequire(import.meta.url);const {migrate,readJSON,loadImported,checkPort,run}=require('../lib/runtime');
const tempBase=fileURLToPath(new URL('./.tmp/',import.meta.url));
async function fixture(t){await fs.mkdir(tempBase,{recursive:true});const base=await fs.mkdtemp(path.join(tempBase,'migration-'));assert.ok(base.startsWith(tempBase));t.after(async()=>{assert.ok(base.startsWith(tempBase));await fs.rm(base,{recursive:true,force:true});});const root=path.join(base,'airplay-mm.0'),legacy=path.join(base,'airplay-mm');await fs.mkdir(root);await fs.mkdir(legacy);return {root,legacy};}
const config={importPrototype:true,network:'10.0.0.0/24',defaultMode:'native-ntp'};
test('migration preserves original files, named devices and counter-free history',async t=>{
    const {root,legacy}=await fixture(t);const history=[{id:'original',status:'completed',text:'Hallo'}];await fs.writeFile(path.join(legacy,'history.json'),JSON.stringify(history));await fs.writeFile(path.join(legacy,'settings.json'),JSON.stringify({target:'saved',volume:15}));await fs.writeFile(path.join(legacy,'config.json'),JSON.stringify({host:'10.0.0.210',network:config.network,modes:{'10.0.0.200':'native-ntp'}}));
    const before=await fs.readFile(path.join(legacy,'history.json'),'utf8');assert.equal(await migrate(root,legacy,config,[{address:'10.0.0.200',name:'Example Soundbar',kind:'soundbar'}]),true);assert.deepEqual(await readJSON(path.join(root,'history.json')),history);assert.equal((await loadImported(root,legacy,false)).devices[0].name,'Example Soundbar');assert.equal(await fs.readFile(path.join(legacy,'history.json'),'utf8'),before);
});
test('repeated migration and existing adapter files are never overwritten',async t=>{
    const {root,legacy}=await fixture(t);await fs.writeFile(path.join(legacy,'history.json'),'[{"id":"old","status":"completed"}]');await fs.writeFile(path.join(root,'history.json'),'[{"id":"new","status":"completed"}]');await migrate(root,legacy,config);await fs.writeFile(path.join(legacy,'history.json'),'[]');assert.equal(await migrate(root,legacy,config),false);assert.equal((await readJSON(path.join(root,'history.json')))[0].id,'new');
});
test('corrupt migration input is reported without altering the source or creating a completion marker',async t=>{
    const {root,legacy}=await fixture(t);await fs.writeFile(path.join(legacy,'history.json'),'{broken');await assert.rejects(migrate(root,legacy,config));assert.equal(await fs.readFile(path.join(legacy,'history.json'),'utf8'),'{broken');await assert.rejects(fs.stat(path.join(root,'prototype-import.json')));
});
test('oversized backup files cannot exhaust memory during migration',async t=>{const {root,legacy}=await fixture(t);await fs.writeFile(path.join(legacy,'history.json'),' '.repeat(500001));await assert.rejects(migrate(root,legacy,config),/Invalid local data file/);});
test('disabled migration leaves both directories untouched',async t=>{const {root,legacy}=await fixture(t);await fs.writeFile(path.join(legacy,'history.json'),'[]');assert.equal(await migrate(root,legacy,{...config,importPrototype:false}),false);assert.deepEqual(await fs.readdir(root),[]);});
test('occupied ports produce a precise instruction and do not stop the existing listener',async()=>{
    const server=net.createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));try{const {port}=server.address();await assert.rejects(checkPort('127.0.0.1',port),/Port .*already in use/);assert.equal(server.listening,true);}finally{await new Promise(resolve=>server.close(resolve));}
});
function processStub(output,code=0){const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.pid=undefined;queueMicrotask(()=>{child.stdout.write(output);child.emit('close',code);});return child;}
test('setup stdout is bounded and only the final JSON result is interpreted',async()=>{const progress=[];const result=await run('fake',[],{spawnFn:()=>processStub('{"event":"setup","message":"Fortschritt"}\n{"installed":true}\n'),onLine:x=>progress.push(x)});assert.equal(result.installed,true);assert.equal(progress[0].message,'Fortschritt');});
test('setup spawn failures and malformed results reject rather than hanging',async()=>{await assert.rejects(run('fake',[],{spawnFn:()=>{throw new Error('missing');}}),/missing/);await assert.rejects(run('fake',[],{spawnFn:()=>processStub('kein JSON\n')}),/valid response/);await assert.rejects(run('fake',[],{spawnFn:()=>processStub('{"error":"offline"}\n',1)}),/offline/);});
test('an aborted setup cannot start another process',async()=>{const abort=new AbortController();abort.abort();let spawned=false;await assert.rejects(run('fake',[],{signal:abort.signal,spawnFn:()=>{spawned=true;}}),/cancelled/);assert.equal(spawned,false);});
