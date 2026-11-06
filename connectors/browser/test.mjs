import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

let created = [], scriptCalls = 0, currentURL = 'https://example.com/page';
let requested=[],removed=[],networkDown=false,responseStatus=200;
const context = vm.createContext({
  URL, AbortSignal, setInterval:()=>{},
  document:{getElementById:()=>({addEventListener:()=>{},textContent:'',hidden:false})},
  fetch:async(url,options)=>{requested.push({url,options});if(networkDown)throw Error('offline');return{ok:responseStatus===200,status:responseStatus,text:async()=>JSON.stringify({ok:true})}},
  chrome:{storage:{local:{get:()=>new Promise(()=>{}),set:async()=>{},remove:async keys=>{removed.push(keys)}}},
    tabs:{create:async options=>{created.push(options);return{id:7}},get:async()=>({url:currentURL})},
    scripting:{executeScript:async()=>{scriptCalls++;return[{result:{text:'Shared text'}}]}}}
});
vm.runInContext(fs.readFileSync(new URL('./popup.js',import.meta.url),'utf8'),context);
async function call(task) { context.task=task; return await vm.runInContext('execute(task)',context); }
await assert.rejects(call({capability:'browser.open',arguments:{url:'javascript:alert(1)'}}));
await assert.rejects(call({capability:'browser.open',arguments:{url:'https://user:secret@example.com'}}));
await assert.rejects(call({capability:'shell.run',arguments:{}}));
await assert.rejects(call({capability:'browser.read',arguments:{}}));
assert.equal(scriptCalls,0);
await call({capability:'browser.open',arguments:{url:'https://example.com'}});
assert.equal(created[0].active,false);
vm.runInContext("shared={id:7,origin:'https://example.com'}",context);
await assert.rejects(call({capability:'browser.read',arguments:{tab_id:8}}));
currentURL='https://other.example/page';
await assert.rejects(call({capability:'browser.read',arguments:{}}));
assert.equal(scriptCalls,0);
currentURL='https://example.com/page';
const result=await call({capability:'browser.read',arguments:{}});
assert.equal(result.text,'Shared text');
assert.equal(scriptCalls,1);
assert.throws(()=>vm.runInContext("serverAddress('http://remote.example')",context));
assert.throws(()=>vm.runInContext("serverAddress('https://secret@example.com')",context));
vm.runInContext("connection={server:'https://example.com',token:'test',node_id:'node'}",context);
await vm.runInContext('disconnect()',context);
assert.equal(requested.at(-1).url,'https://example.com/api/node/disconnect');
assert.equal(vm.runInContext('connection',context),null);
assert.equal(removed.at(-1).join(','),'connection,shared');
networkDown=true;
vm.runInContext("connection={server:'https://example.com',token:'test',node_id:'node'}",context);
await vm.runInContext('disconnect()',context);
assert.equal(vm.runInContext('connection.revocation_pending',context),true);
const attempts=requested.length;
await vm.runInContext('tick()',context);
assert.equal(requested.length,attempts,'Pending disconnect must not claim work');
networkDown=false;responseStatus=401;
await vm.runInContext('disconnect()',context);
assert.equal(vm.runInContext('connection',context),null,'Already revoked is safe to forget');
console.log('Browser connector scope, full disconnect, offline retry, and revoked-credential checks passed.');
