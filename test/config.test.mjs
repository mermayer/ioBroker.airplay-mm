import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {interfaces} from './helpers.mjs';
const require=createRequire(import.meta.url);
const {normalizeConfig,privateIPv4,inNetwork}=require('../lib/config');
const config=(data={},instance=0,imported={})=>normalizeConfig(data,instance,imported,interfaces);

test('warm connection settings have bounded types and retain an explicit zero',()=>{
    assert.equal(config().keepAliveEnabled,true);assert.equal(config().keepAliveSeconds,300);
    assert.equal(config({keepAliveEnabled:false,keepAliveSeconds:0}).keepAliveSeconds,0);
    for(const keepAliveSeconds of [-1,3601,1.2,NaN,'300',true])assert.throws(()=>config({keepAliveSeconds}));
    for(const keepAliveEnabled of ['true',1])assert.throws(()=>config({keepAliveEnabled}));
});
test('automatic interface selection uses its actual subnet',()=>{const x=config();assert.equal(x.host,'10.0.0.210');assert.equal(x.network,'10.0.0.210/24');assert.deepEqual(x.seedHosts,[]);assert.equal(x.defaultMode,'native-ntp');});
test('only existing private local interfaces may bind',()=>{for(const bind of ['0.0.0.0','8.8.8.8','10.0.0.111','example.org','::1'])assert.throws(()=>config({bind}));});
test('invalid networks and public seed addresses are rejected',()=>{for(const network of ['0.0.0.0/0','10.0.0.0/99','10.0.0.0/24/1','10.0.1.0/24'])assert.throws(()=>config({network}));for(const seedHosts of [['8.8.8.8'],['10.0.1.2'],['10.0.0.200; echo x'],Array(33).fill('10.0.0.200')])assert.throws(()=>config({seedHosts}));});
test('boundaries reject non-finite or coerced ports and volumes',()=>{for(const port of [80,65536,NaN,'8098',true])assert.throws(()=>config({port}));for(const maxVolume of [0,101,NaN,'40',true])assert.throws(()=>config({maxVolume}));assert.equal(config({maxVolume:100}).maxVolume,100);});
test('migration keeps tested modes, aliases and receiver addresses',()=>{const old={host:'10.0.0.210',network:'10.0.0.0/24',seedHosts:['10.0.0.200'],modes:{'10.0.0.200':'native-ntp'},devices:[{address:'10.0.0.200',name:'Example Soundbar',kind:'soundbar'}]};const x=config({},0,old);assert.equal(x.presets[0].name,'Example Soundbar');assert.equal(x.modes['10.0.0.200'],'native-ntp');assert.equal(config({port:8099},0,old).port,8099);});
test('device duplicates, modes, names and symbols are validated',()=>{const row={address:'10.0.0.200',name:'LG',kind:'soundbar'};for(const devices of [[row,row],[{...row,name:''}],[{...row,mode:'unsafe'}],[{...row,kind:'invalid'}]])assert.throws(()=>config({devices}));});
test('subsequent instances cannot take ownership of the prototype states',()=>{assert.throws(()=>config({legacyBridge:true},1));assert.equal(config({legacyBridge:false},1).importPrototype,false);assert.equal(config({legacyBridge:false},1).port,8099);});
test('CIDR membership respects signed high IPv4 bits',()=>{assert.equal(inNetwork('10.0.0.200','10.0.0.0/24'),true);assert.equal(inNetwork('10.0.1.200','10.0.0.0/24'),false);assert.equal(privateIPv4('172.31.0.1'),true);assert.equal(privateIPv4('172.32.0.1'),false);});
test('disabling import allows a changed network without inheriting stale prototype addresses',()=>{const x=config({importPrototype:false,bind:'10.0.0.210',network:'10.0.0.0/24'},0,{host:'10.0.1.1',network:'10.0.1.0/24',seedHosts:['10.0.1.2']});assert.deepEqual(x.seedHosts,[]);assert.deepEqual(x.presets,[]);});
