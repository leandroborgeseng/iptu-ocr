const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto'),fs=require('node:fs'),vm=require('node:vm');
const ctx=vm.createContext({self:{},Uint32Array,Uint8Array,DataView});const SHA=vm.runInContext(fs.readFileSync('app/static/sha256-worker.js','utf8')+'\nSHA256;',ctx);
for(const size of [0,1,3,55,56,63,64,65,127,128,129,4097,1048576])test('SHA-256 incremental matches Node crypto at '+size+' bytes',()=>{const bytes=crypto.randomBytes(size),h=new SHA();for(let i=0;i<size;i+=37)h.update(bytes.subarray(i,i+37));assert.equal(h.hex(),crypto.createHash('sha256').update(bytes).digest('hex'));});
test('standard abc vector',()=>assert.equal(new SHA().update(Buffer.from('abc')).hex(),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'));
