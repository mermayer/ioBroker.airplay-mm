import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),{generate,keyFor,validateRoot}=require('../lib/sayit-source');
const base=path.resolve(import.meta.dirname,'.tmp');fs.mkdirSync(base,{recursive:true});
async function fixture(action){const folder=fs.mkdtempSync(path.join(base,'sayit-')),root=path.join(folder,'airplay-mm.0');fs.mkdirSync(path.join(root,'cache'),{recursive:true});try{await action(root);}finally{assert.ok(fs.realpathSync(folder).startsWith(fs.realpathSync(base)+path.sep));fs.rmSync(folder,{recursive:true});}}
function loader(calls=[],failAt=-1){
    class Generator{
        constructor(adapter,options){this.options=options;assert.equal(adapter.config.engine,'de');assert.equal(adapter.config.cache,false);assert.equal(adapter.setState,undefined);}
        static getMaxTextLength(){return 5;}
        static splitText(text){return text.match(/.{1,5}/gu);}
        async sayItGetSpeech(props){calls.push(props);if(calls.length===failAt)throw Error('Ausfall');fs.writeFileSync(this.options.MP3FILE,Buffer.alloc(101,calls.length));return this.options.MP3FILE;}
    }
    return ()=>({Generator,version:'5.3.2'});
}
test('actual SayIt generator contract handles every text part without an adapter or player',()=>fixture(async root=>{
    const calls=[],result=await generate(root,'Hallo Küche',loader(calls));
    assert.equal(result.sayitVersion,'5.3.2');assert.equal(result.provider,'sayit-google');assert.equal(result.key,keyFor('Hallo Küche'));
    assert.equal(calls.map(x=>x.text).join(''),'Hallo Küche');assert.equal(result.chunks.length,calls.length);
    assert.ok(calls.every(x=>x.language==='de'&&x.testOptions.engine==='de'));
    assert.deepEqual(fs.readdirSync(path.join(root,'cache')),[]);
}));
test('a failed part is reported once and partial files are removed',()=>fixture(async root=>{
    const calls=[];await assert.rejects(generate(root,'Längerer Text',loader(calls,2)),/Ausfall/);assert.equal(calls.length,2);assert.deepEqual(fs.readdirSync(path.join(root,'cache')),[]);
}));
test('Google and Piper cache keys differ for identical text',()=>{
    const {createHash}=require('node:crypto');const text='Hallo';assert.notEqual(keyFor(text),createHash('sha256').update('piper-1.8.0:thorsten-medium:'+text).digest('hex'));assert.match(keyFor(text),/^[0-9a-f]{64}$/);
});
test('invalid text or a foreign root cannot reach the SayIt generator',()=>fixture(async root=>{
    let loaded=0;for(const text of ['',null,'x'.repeat(501),'Hallo\x00'])await assert.rejects(generate(root,text,()=>{loaded++;}));assert.equal(loaded,0);assert.throws(()=>validateRoot(path.dirname(root)),/data directory/);
}));
test('an incompatible SayIt text limit is rejected before generation',()=>fixture(async root=>{
    class Changed{static getMaxTextLength(){return 500;}}
    await assert.rejects(generate(root,'Hallo',()=>({Generator:Changed,version:'new'})),/text limit/);
}));
