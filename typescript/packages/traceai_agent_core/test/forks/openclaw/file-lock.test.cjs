const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {join} = require('node:path');
const {tmpdir} = require('node:os');
const {spawn} = require('node:child_process');
const http = require('node:http');
const c = require('../../../dist/src/index.js');

async function fixture(t) {
  const home = await fs.mkdtemp(join(tmpdir(),'fi-lock-'));
  const requests = [];
  const server = http.createServer(async(req,res) => {
    let body=''; for await(const chunk of req) body += chunk;
    requests.push(JSON.parse(body)); res.setHeader('content-type','application/json');res.end('{}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await fs.rm(home,{recursive:true,force:true});});
  const options={agent:'openclaw',home,env:{}};
  await c.setup({...options,configureOnly:true,credentials:{apiKey:'synthetic-public',secretKey:'synthetic-private'},
    otlpEndpoint:`http://127.0.0.1:${server.address().port}/traces`});
  const config=await c.resolveConfig(options);
  return {home,options,config,requests};
}
async function writer(t, path, mode) {
  const script=`
    const fs=require('node:fs/promises');
    const c=require(${JSON.stringify(require.resolve('../../../dist/src/index.js'))});
    (async()=>{
      if(process.argv[2]==='legacy') {
        await fs.writeFile(process.argv[1]+'.lock','',{flag:'wx',mode:0o600});
        process.send('locked'); setInterval(()=>{},1000); return;
      }
      const rename=fs.rename;
      fs.rename=async(...args)=>{
        process.send('locked');
        if(process.argv[2]==='killed') await new Promise(()=>{});
        else await new Promise(r=>setTimeout(r,250));
        return rename(...args);
      };
      await c.updateJson(process.argv[1],m=>({...m,writerField:42}));
      process.disconnect();
    })().catch(e=>{console.error(e);process.exit(1);});`;
  const child=spawn(process.execPath,['-e',script,path,mode],{stdio:['ignore','ignore','pipe','ipc']});
  let errors='';child.stderr.on('data',b=>errors+=b);
  const closed=new Promise(resolve=>child.once('close',(code,signal)=>resolve({code,signal})));
  t.after(async()=>{if(child.exitCode===null && child.signalCode===null)child.kill('SIGKILL');await closed;});
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('writer did not acquire lock: '+errors)),10000);
    child.once('message',()=>{clearTimeout(timer);resolve();});
    child.once('error',error=>{clearTimeout(timer);reject(error);});
    child.once('exit',code=>{clearTimeout(timer);reject(new Error('writer exited '+code+': '+errors));});
  });
  return {child,closed};
}
for (const mode of ['killed','legacy']) test(`disable recovers a ${mode} writer lock and fences an existing exporter`, {timeout:20000}, async t=>{
  const f=await fixture(t), tracer=c.createTurnTracer({config:f.config});
  t.after(()=>tracer.shutdown());
  const {child,closed}=await writer(t,f.config.paths.manifest,mode);
  child.kill('SIGKILL');await closed;
  const start=Date.now();
  await c.disable(f.options);
  assert.ok(Date.now()-start<10000,'bounded stale recovery');
  assert.equal(c.collectionEnabled(f.config),false);
  assert.equal((await c.resolveConfig(f.options)).enabled,false);
  const count=f.requests.length;
  tracer.startTurn({sessionId:'s',turnId:'after-disable'}).end();
  await tracer.flush();
  assert.equal(f.requests.length,count,'pre-existing exporter cannot send after the fence');
  assert.equal((await c.readJson(f.config.paths.manifest)).disabled.openclaw,true);
});

test('disable waits for a live writer; concurrent merges do not lose unrelated fields', {timeout:20000}, async t=>{
  const f=await fixture(t);
  const {closed}=await writer(t,f.config.paths.manifest,'live');
  await Promise.all([c.disable(f.options),...Array.from({length:12},(_,i)=>c.updateJson(f.config.paths.manifest,m=>({...m,['field'+i]:i})))]);
  assert.equal((await closed).code,0);
  const manifest=await c.readJson(f.config.paths.manifest);
  assert.equal(manifest.writerField,42);
  assert.equal(manifest.disabled.openclaw,true);
  assert.equal(manifest.generations.openclaw,f.config.generation+1);
  for(let i=0;i<12;i++)assert.equal(manifest['field'+i],i);
});

test('a busy diagnostic lock does not extend terminal exporter shutdown', {timeout:10000}, async t=>{
  const f=await fixture(t);
  const metadata=c.deliveryStatePath(f.config);
  await fs.mkdir(f.config.paths.stateDir,{recursive:true,mode:0o700});
  const {child,closed}=await writer(t,metadata,'killed');
  const tracer=c.createTurnTracer({config:f.config});
  tracer.startTurn({sessionId:'s',turnId:'metadata-contention'}).end();
  const start=Date.now();
  await tracer.shutdown();
  assert.ok(Date.now()-start<1500,'metadata lock wait fits inside terminal drain budget');
  assert.equal(f.requests.filter(r=>r.resourceSpans?.length).length,1);
  child.kill('SIGKILL');await closed;
});

test('stale recovery never follows lock symlinks or removes foreign content', {timeout:25000}, async t=>{
  const f=await fixture(t), target=join(f.home,'target'), lock=f.config.paths.manifest+'.write-lock';
  await fs.mkdir(target,{mode:0o700});
  await fs.utimes(target,new Date(0),new Date(0));
  await fs.symlink(target,lock);
  await assert.rejects(c.disable(f.options),/UNSAFE_LOCK/);
  assert.equal((await fs.lstat(target)).isDirectory(),true);
  await fs.unlink(lock);
  await fs.writeFile(lock,'not our empty legacy lock',{mode:0o600});
  await fs.utimes(lock,new Date(0),new Date(0));
  await assert.rejects(c.disable(f.options),/UNSAFE_LOCK/);
  assert.equal(await fs.readFile(lock,'utf8'),'not our empty legacy lock');
});
