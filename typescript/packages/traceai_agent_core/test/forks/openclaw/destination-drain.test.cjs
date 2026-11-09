const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
const c = require('../../../dist/src/index.js');

async function fixture(t, handler) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(),'fi-agent-regression-'));
  const requests = [];
  const server = http.createServer(async (req,res) => {
    const chunks=[]; for await (const chunk of req) chunks.push(chunk);
    const body=JSON.parse(Buffer.concat(chunks)); requests.push(body);
    if (handler) await handler(req,res,requests.length);
    if (!res.destroyed) { res.setHeader('content-type','application/json'); res.end('{}'); }
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await fs.rm(home,{recursive:true,force:true});});
  const endpoint=`http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  const env={FI_API_KEY:'synthetic-public',FI_SECRET_KEY:'synthetic-private',FI_AGENT_ENABLED:'true'};
  const options={agent:'openclaw',home,env};
  return {home,endpoint,env,options,requests};
}

test('environment keys do not approve a repository-selected transport; explicit destinations and saved approval do',async t=>{
  const f=await fixture(t);
  const projectDir=path.join(f.home,'repository'); await fs.mkdir(projectDir);
  const options={...f.options,projectDir};
  const paths=await c.configPaths(options);
  await c.writeProtected(paths.project,JSON.stringify({openclaw:{schemaVersion:1,enabled:true,transport:{endpoint:f.endpoint}}}));
  const blocked=await c.resolveConfig(options);
  assert.equal(blocked.provenance.transport,'project');
  assert.equal(blocked.destinationApproved,false);
  assert.equal(blocked.enabled,false);
  assert.ok(blocked.issues.includes('DESTINATION_NOT_APPROVED'));
  const tracer=c.createTurnTracer({config:blocked});
  tracer.startTurn({sessionId:'s',turnId:'t',input:'never export'}).end();
  await tracer.shutdown();
  assert.equal(f.requests.length,0);
  for (const explicit of [
    {...options,env:{...f.env,FI_OTLP_ENDPOINT:f.endpoint}},
    {...options,env:{...f.env,FI_BASE_URL:new URL(f.endpoint).origin}},
    {...options,flags:{transport:{endpoint:f.endpoint}}},
  ]) {
    const config=await c.resolveConfig(explicit);
    assert.equal(config.destinationApproved,true);
    assert.equal(config.enabled,true);
  }
  await c.setup({...f.options,otlpEndpoint:f.endpoint,configureOnly:true});
  const approved=await c.resolveConfig(options);
  assert.equal(approved.destinationApproved,true,'matching protected profile authorizes the route');
  await c.writeProtected(paths.project,JSON.stringify({openclaw:{schemaVersion:1,transport:{endpoint:f.endpoint+'/changed'}}}));
  assert.equal((await c.resolveConfig(options)).destinationApproved,false,'environment keys cannot override a changed destination');
});

test('environment keys cannot approve repository selection of another protected profile',async t=>{
  const f=await fixture(t);
  await c.setup({...f.options,otlpEndpoint:f.endpoint,configureOnly:true});
  const projectDir=path.join(f.home,'repository');await fs.mkdir(projectDir);
  const options={...f.options,projectDir}, paths=await c.configPaths(options);
  await c.updateJson(paths.credentials,store=>({...store,profiles:{...store.profiles,other:{...Object.values(store.profiles)[0],endpoint:f.endpoint+'/other'}}}));
  await c.writeProtected(paths.project,JSON.stringify({openclaw:{schemaVersion:1,profile:'other'}}));
  assert.equal((await c.resolveConfig(options)).destinationApproved,false);
});

test('terminal shutdown joins in-flight export then drains all completed batches and receipts',async t=>{
  let started;
  const firstStarted=new Promise(resolve=>started=resolve);
  const f=await fixture(t,async(_req,_res,n)=>{if(n===1){started();await new Promise(resolve=>setTimeout(resolve,200));}});
  const config=await c.resolveConfig({...f.options,env:{...f.env,FI_OTLP_ENDPOINT:f.endpoint}});
  const processor=new c.BoundedSpanProcessor(config);
  const provider=new BasicTracerProvider({spanProcessors:[processor]});
  const tracer=provider.getTracer('test');
  tracer.startSpan('context').end();
  const periodic=processor.forceFlush();
  await firstStarted;
  for(let i=0;i<130;i++)tracer.startSpan('completed-'+i).end();
  const closing=processor.shutdown();
  assert.equal(processor.shutdown(),closing,'terminal shutdown is idempotent');
  // Diagnostic completion can race the lifecycle notification, not only the HTTP timer.
  tracer.startSpan('root-finished-during-drain').end();
  await closing; await periodic;
  assert.equal(processor.state.pending,0);
  assert.equal(processor.state.exported,132);
  assert.equal(processor.state.dropped,0);
  assert.equal(f.requests.length,4);
  const receipt=await c.readJson(c.deliveryStatePath(config));
  assert.equal(receipt.exported,132);
});

test('a service lifecycle drain does not close the provider before later run spans',async t=>{
  const f=await fixture(t);
  const config=await c.resolveConfig({...f.options,env:{...f.env,FI_OTLP_ENDPOINT:f.endpoint}});
  const processor=new c.BoundedSpanProcessor(config);
  const provider=new BasicTracerProvider({spanProcessors:[processor]});
  const tracer=provider.getTracer('test');
  tracer.startSpan('context').end();await processor.drain();
  tracer.startSpan('model').end();tracer.startSpan('root').end();await processor.drain();
  await processor.shutdown();
  assert.equal(processor.state.exported,3);
  assert.equal(processor.state.dropped,0);
});

test('one shutdown budget bounds active and queued work and persists deadline drops',async t=>{
  let started;
  const firstStarted=new Promise(resolve=>started=resolve);
  const f=await fixture(t,async()=>{started();await new Promise(resolve=>setTimeout(resolve,1000));});
  const config=await c.resolveConfig({...f.options,env:{...f.env,FI_OTLP_ENDPOINT:f.endpoint}});
  const processor=new c.BoundedSpanProcessor(config);
  const provider=new BasicTracerProvider({spanProcessors:[processor]});
  const tracer=provider.getTracer('test');
  tracer.startSpan('active').end();const periodic=processor.forceFlush();
  await firstStarted;
  for(let i=0;i<70;i++)tracer.startSpan('queued-'+i).end();
  const began=Date.now(); await processor.shutdown(500); await periodic;
  assert.ok(Date.now()-began<900,'not one deadline per batch');
  assert.equal(f.requests.length,1,'queued spans must not launch after the shared deadline');
  assert.equal(processor.state.pending,0);
  assert.equal(processor.state.dropped,71);
  assert.equal(processor.state.lastError,'SHUTDOWN_BUDGET');
  const receipt=await c.readJson(c.deliveryStatePath(config));
  assert.equal(receipt.dropped,71);
  assert.equal(receipt.lastError,'SHUTDOWN_BUDGET');
});


test('unsafe external-volume project layer falls back to the protected user route and reports it', async t => {
  const f=await fixture(t);
  await c.setup({...f.options,otlpEndpoint:f.endpoint,configureOnly:true});
  const volume=path.join(f.home,'shared-volume'); await fs.mkdir(volume); await fs.chmod(volume,0o775);
  const projectDir=path.join(volume,'project'); await fs.mkdir(projectDir);
  await fs.mkdir(path.join(projectDir,'.futureagi'));
  await fs.writeFile(path.join(projectDir,'.futureagi','agents.json'),JSON.stringify({openclaw:{schemaVersion:1,transport:{endpoint:'https://unapproved.invalid/traces'}}}));
  const options={...f.options,projectDir};
  const config=await c.resolveConfig(options);
  assert.equal(config.enabled,true);
  assert.equal(config.endpoint,f.endpoint);
  assert.ok(config.issues.includes('PROJECT_CONFIG_IGNORED'));
  assert.ok((await c.inspectStatus(options)).issues.includes('PROJECT_CONFIG_IGNORED'));
  await fs.chmod(volume,0o700);
  assert.equal((await c.resolveConfig(options)).destinationApproved,false,'a readable repository still cannot redirect keys');
  const projectFile=path.join(projectDir,'.futureagi','agents.json');
  await fs.unlink(projectFile);
  await fs.symlink(config.paths.credentials,projectFile);
  const symlinked=await c.resolveConfig(options);
  assert.equal(symlinked.endpoint,f.endpoint);
  assert.ok(symlinked.issues.includes('PROJECT_CONFIG_IGNORED'));
  // The same fallback never applies to the user's credential directory.
  await fs.chmod(config.paths.configDir,0o775);
  await assert.rejects(c.resolveConfig(options),/UNSAFE_DIRECTORY_MODE/);
});
