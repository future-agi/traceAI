const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {writeProtectedUnderLock,readProtected,contentHash}=require('../../../dist/src/files');
const {redact}=require('../../../dist/src/privacy');

test('externally locked atomic writes retain private mode, no-follow and optimistic checks',async t=>{
 const home=await fs.mkdtemp(path.join(os.tmpdir(),'fi-atomic-protected-'));t.after(()=>fs.rm(home,{recursive:true,force:true}));
 const file=path.join(home,'checkpoint.json'),other=path.join(home,'other.json');
 await writeProtectedUnderLock(file,'old',null);assert.equal((await fs.stat(file)).mode&0o777,0o600);
 await assert.rejects(writeProtectedUnderLock(file,'bad',null),/CONCURRENT_SAVE/);
 await assert.rejects(writeProtectedUnderLock(file,'bad',contentHash('different')),/CONCURRENT_SAVE/);
 assert.equal(await readProtected(file),'old');await writeProtectedUnderLock(file,'new',contentHash('old'));
 await fs.writeFile(other,'do not replace',{mode:0o600});await fs.unlink(file);await fs.symlink(other,file);
 await assert.rejects(writeProtectedUnderLock(file,'bad'));assert.equal(await fs.readFile(other,'utf8'),'do not replace');
 await fs.unlink(file);await fs.link(other,file);await assert.rejects(writeProtectedUnderLock(file,'bad'),/UNSAFE_FILE/);
 await fs.unlink(file);await fs.chmod(home,0o777);await assert.rejects(writeProtectedUnderLock(file,'bad'),/UNSAFE_DIRECTORY_MODE/);
});
test('quoted redaction respects nested JSON escape depth including embedded quotes and trailing slashes',()=>{
 for(const secret of ['prefix-secret"suffix-secret','prefix-secret\\"suffix-secret\\','prefix-secret\\\\"suffix-secret\\\\']){
   let encoded=JSON.stringify({password:secret,safe:'safe-field'});
   for(let depth=0;depth<6;depth++){
     const result=redact('log '+encoded+' tail-kept');
     assert.ok(!result.includes('prefix-secret'));assert.ok(!result.includes('suffix-secret'));
     assert.ok(result.includes('[REDACTED]'));assert.ok(result.includes('safe-field'));assert.ok(result.endsWith('tail-kept'));
     encoded=JSON.stringify(encoded);
   }
 }
 assert.equal(redact('log password="prefix-secret\\"suffix-secret'), 'log password="[REDACTED]"');
});
test('standard secret-access and private-key aliases are recognized without hiding public keys',()=>{
 const result=redact('AWS_SECRET_ACCESS_KEY="synthetic-sensitive-aws" private_key="synthetic-sensitive-private" public_key="public-kept"');
 assert.ok(!result.includes('synthetic-sensitive'));assert.ok(result.includes('public-kept'));
});
