import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const pkg=require('../package.json'),io=require('../io-package.json'),config=require('../admin/jsonConfig.json');
test('package entry, version, license, daemon and declared backup folder agree',async()=>{
    assert.equal(pkg.name,'iobroker.'+io.common.name);assert.equal(pkg.version,io.common.version);assert.equal(pkg.license,io.common.licenseInformation.license);assert.equal(io.common.dataFolder,'airplay-mm.%INSTANCE%');assert.equal(io.common.mode,'daemon');assert.equal(io.common.compact,false);assert.equal(io.common.enabled,false);assert.equal(io.common.messagebox,true);await fs.access(new URL('../'+pkg.main,import.meta.url));await fs.access(new URL('../admin/'+io.common.icon,import.meta.url));
});
test('every real configuration property has a documented default and no hidden credentials',()=>{
    const names=[];function walk(items){for(const [name,row]of Object.entries(items||{})){if(row.type==='panel'){walk(row.items);}else if(!name.startsWith('_'))names.push(name);}}walk(config.items);
    assert.deepEqual(names.sort(),Object.keys(io.native).sort());assert.equal(config.type,'tabs');assert.equal(io.common.adminUI.config,'materialize');assert.equal(io.native.maxVolume,40);assert.ok(!Object.keys(io.native).some(k=>/token|secret|password/i.test(k)));
});
test('automatic install is confined to its own directory and needs no root or global system service',async()=>{
    const source=await fs.readFile(new URL('../worker/setup_runtime.py',import.meta.url),'utf8');assert.ok(source.includes("'--without-pip'"));assert.ok(source.includes('ASSETS'));assert.ok(!/subprocess\.(?:run|Popen)\(\[.(?:sudo|apt|systemctl)/.test(source));assert.ok(!source.includes('shell=True'));
});
test('declared ioBroker runtime modules resolve',()=>{
    for(const name of Object.keys(pkg.dependencies)){assert.ok(require.resolve(name));}assert.equal(pkg.dependencies['@iobroker/adapter-core'],'3.4.2');assert.equal(pkg.dependencies['@iobroker/types'],'7.2.5');
});
