const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const source=readFileSync('app/static/connection-profile.js','utf8');
function boot({hash='',draft,blocked=false}={}){
 const values=new Map();if(draft)values.set('iptu.connection.draft.v1',JSON.stringify(draft));
 const form={elements:Object.fromEntries(['server','share','subpath','domain','username','password','stable_seconds'].map(k=>[k,{value:k==='username'?'previous-user':k==='password'?'secret-not-to-store':k==='stable_seconds'?'120':''}]))};
 const ctx={window:{},URLSearchParams,location:{hash,pathname:'/',search:''},history:{replaceState(...args){ctx.replaced=args[2];}},localStorage:{getItem(k){if(blocked)throw Error();return values.get(k);},setItem(k,v){if(blocked)throw Error();values.set(k,v);}}};
 vm.runInNewContext(source,ctx);return {ctx,form,values,api:ctx.window.IPTUConnectionProfile};
}
test('setup link fills connection while preserving existing user and password',()=>{
 const {api,form,ctx}=boot({hash:'#conexao?server=windows&share=docs&subpath=Files%2FIPTU&domain=domain'});
 const outcome=api.apply(form);assert.equal(outcome.linked,true);assert.equal(form.elements.subpath.value,'Files/IPTU');assert.equal(form.elements.username.value,'previous-user');assert.equal(form.elements.password.value,'secret-not-to-store');assert.equal(ctx.replaced,'/#conexao');
});
test('password is never saved or restored, including from a malicious old draft',()=>{
 const {api,form,values}=boot({draft:{server:'windows',password:'malicious-password'}});
 api.apply(form);assert.equal(form.elements.password.value,'secret-not-to-store');api.save(form);
 const saved=values.get('iptu.connection.draft.v1');assert.equal(saved.includes('password'),false);assert.equal(saved.includes('secret-not-to-store'),false);
});
test('draft keeps input after a reload and profile takes priority over draft',()=>{
 const {api,form}=boot({draft:{server:'old-server',domain:'old-domain'},hash:'#conexao?server=new-server'});
 api.apply(form);assert.equal(form.elements.server.value,'new-server');assert.equal(form.elements.domain.value,'old-domain');
});
test('restricted browser storage does not prevent server-side configuration',()=>{
 const {api,form}=boot({blocked:true,hash:'#conexao?server=windows'});
 assert.doesNotThrow(()=>api.apply(form));assert.doesNotThrow(()=>api.save(form));assert.equal(form.elements.server.value,'windows');
});
