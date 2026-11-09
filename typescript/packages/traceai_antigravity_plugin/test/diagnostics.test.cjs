const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const core = require('@traceai/agent-core');
const {handleEvent} = require('../dist/runtime');
const cli = path.resolve(__dirname, '../../traceai_agents/dist/cli.js');

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-agy-diagnostics-'));
  const project = path.join(home,'project');
  const bin = path.join(home,'bin');
  await fs.mkdir(project,{mode:0o700}); await fs.mkdir(bin,{mode:0o700});
  // Deterministic host-registration fixture; runtime/provider E2E uses the real binary separately.
  await fs.writeFile(path.join(bin,'agy'), `#!${process.execPath}\nif(process.argv[2]==='--version') console.log('1.5.2'); else if(process.argv.slice(2).join(' ')==='plugin list --json') console.log(process.env.AGY_FIXTURE_LIST || '[]'); else process.exit(1);\n`, {mode:0o700});
  const requests=[];
  const server = http.createServer((req,res) => {
    const chunks=[]; req.on('data',c=>chunks.push(c)); req.on('end',()=>{
      requests.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.setHeader('Content-Type','application/json'); res.end('{}');
    });
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  t.after(async()=>{server.closeAllConnections();await new Promise(r=>server.close(r));await fs.rm(home,{recursive:true,force:true});});
  const env={HOME:home,PATH:bin+path.delimiter+process.env.PATH,FI_API_KEY:'synthetic-api',FI_SECRET_KEY:'synthetic-secret',
    FI_OTLP_ENDPOINT:`http://127.0.0.1:${server.address().port}/tracer/v1/traces`};
  const run = (args,cwd=project) => new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[cli,...args],{env,cwd,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr=''; child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);
    child.on('error',reject);child.on('close',code=>resolve({code,stdout,stderr}));
  });
  assert.equal((await run(['setup','antigravity','--configure-only','--json'])).code,0);
  const options={agent:'antigravity',home,env,projectDir:project};
  const paths=await core.configPaths(options);
  async function registration(enabled=true) {
    const installedPath=path.join(home,'.gemini/config/plugins/futureagi-tracing');
    await fs.mkdir(installedPath,{recursive:true,mode:0o700});
    const hashes={};
    for(const file of ['plugin.json','hooks.json','hook.cjs']) {
      const content='synthetic '+file;
      await fs.writeFile(path.join(installedPath,file),content,{mode:0o600});hashes[file]=core.contentHash(content);
    }
    await core.writeProtected(path.join(paths.configDir,'antigravity-registration.json'),JSON.stringify({schemaVersion:1,hostVersion:'1.5.2',installedPath,hashes}));
    env.AGY_FIXTURE_LIST=JSON.stringify([{name:'futureagi-tracing',path:installedPath,enabled}]);
    await core.writeProtected(core.deliveryStatePath(await core.resolveConfig(options)),JSON.stringify({pending:0,exported:0,dropped:0,activatedAt:new Date().toISOString()}));
  }
  return {home,project,env,options,paths,requests,run,registration};
}

test('status and doctor use the hook workspace privacy and enabled overrides by default',async t=>{
  const f=await fixture(t); await f.registration();
  await core.writeProtected(f.paths.project,JSON.stringify({antigravity:{schemaVersion:1,enabled:false,captureContent:false}}));
  for(const command of ['status','doctor']) {
    const result=await f.run([command,'antigravity','--json']);
    const status=JSON.parse(result.stdout);
    const effective=await core.resolveConfig(f.options);
    assert.equal(result.code,2,result.stderr);
    assert.equal(status.enabled,effective.enabled);assert.equal(status.privacy,'structure-only');
    assert.equal(status.provenance.enabled,'project');assert.equal(status.provenance.captureContent,'project');
  }
  const before=f.requests.length;
  const payload={conversationId:'workspace-test',workspacePaths:[f.project],initialNumSteps:1,invocationNum:0,
    transcriptPath:path.join(f.home,'.gemini/antigravity-cli/brain/workspace-test/.system_generated/logs/transcript_full.jsonl')};
  await handleEvent('PreInvocation',payload,{home:f.home,env:f.env});
  await handleEvent('Stop',{...payload,fullyIdle:true,terminationReason:'NO_TOOL_CALL'},{home:f.home,env:f.env});
  assert.equal(f.requests.length,before,'disabled hook must not export');
  await assert.rejects(fs.stat(path.join(f.paths.stateDir,'antigravity-conversations')),{code:'ENOENT'});
  const explicit=await f.run(['status','antigravity','--json','--project-dir',f.home]);
  assert.equal(explicit.code,0,explicit.stdout+explicit.stderr);
  assert.equal(JSON.parse(explicit.stdout).enabled,true);
  assert.equal(JSON.parse(explicit.stdout).privacy,'content-on-by-default');
});

test('status exposes unresolved pending source turns separately from export counters',async t=>{
  const f=await fixture(t);await f.registration();
  const payload={conversationId:'pending',workspacePaths:[f.project],initialNumSteps:1,invocationNum:0,lastUserInput:'pending prompt',
    transcriptPath:path.join(f.home,'.gemini/antigravity-cli/brain/pending/.system_generated/logs/transcript_full.jsonl')};
  await handleEvent('PreInvocation',payload,{home:f.home,env:f.env});
  const result=await f.run(['status','antigravity','--json']),status=JSON.parse(result.stdout);
  assert.equal(result.code,2);assert.equal(status.pendingTurns,1);assert.equal(status.delivery.exported,0);
  assert.ok(status.issues.includes('PENDING_TURNS'));assert.ok(status.diagnostics.some(d=>d.code==='PENDING_TURNS'));
});

test('CLI legacy hook setup and status explain how to remove duplicate tracing',async t=>{
  const f=await fixture(t);
  await core.writeProtected(path.join(path.dirname(f.paths.user),'hooks.json'),JSON.stringify({legacy:'fi-hook-to-otlp'}));
  for(const command of ['setup','status']) {
    const result=await f.run([command,'antigravity','--json']);
    assert.equal(result.code,2,result.stderr);
    const output=JSON.parse(result.stdout);
    assert.ok(output.diagnostics.some(d=>d.code==='REMOVE_LEGACY_FUTUREAGI_HOOKS'&&d.action.includes('hooks.json')));
  }
  const human=await f.run(['setup','antigravity']);
  assert.match(human.stderr,/Remove only the old Future AGI tracing entries/);
  assert.ok(!human.stderr.includes('PACKAGE_PUBLICATION_AND_CLOUD_READBACK_REQUIRED'));
});

test('documented setup refuses workspace legacy exporter without --project-dir',async t=>{
  const f=await fixture(t);
  await core.writeProtected(path.join(f.project,'.agents/hooks.json'),JSON.stringify({legacy:'fi-hook-to-otlp'}));
  const result=await f.run(['setup','antigravity','--project','coding-agents','--json']);
  assert.equal(result.code,2,result.stderr);
  const output=JSON.parse(result.stdout);
  assert.equal(output.registered,false);
  assert.ok(output.diagnostics.some(d=>d.code==='REMOVE_LEGACY_FUTUREAGI_HOOKS'),JSON.stringify(output));
  await assert.rejects(fs.stat(path.join(f.home,'.gemini/config/plugins/futureagi-tracing')),{code:'ENOENT'});
});

test('doctor diagnoses a missing pinned interpreter',async t=>{
  const f=await fixture(t);await f.registration();
  const file=path.join(f.paths.configDir,'antigravity-registration.json');
  const receipt=JSON.parse(await fs.readFile(file,'utf8'));receipt.node=path.join(f.home,'missing-node');
  await core.writeProtected(file,JSON.stringify(receipt));
  const result=await f.run(['doctor','antigravity','--json']);
  assert.equal(result.code,2);
  assert.ok(JSON.parse(result.stdout).diagnostics.some(d=>d.code==='PINNED_NODE_MISSING'));
});
test('workspace duplicate preflight still works on external-volume permissions',async t=>{
  const f=await fixture(t);
  await core.writeProtected(path.join(f.project,'.agents/hooks.json'),JSON.stringify({legacy:'fi-hook-to-otlp'}));
  await fs.chmod(f.project,0o775);
  const result=await f.run(['setup','antigravity','--project','coding-agents','--json']);
  assert.equal(result.code,2);
  assert.ok(JSON.parse(result.stdout).diagnostics.some(d=>d.code==='REMOVE_LEGACY_FUTUREAGI_HOOKS'));
});

test('CLI host-disabled and unowned plugins retain actionable, distinct diagnostics',async t=>{
  const f=await fixture(t);await f.registration(false);
  const disabled=await f.run(['status','antigravity','--json']);
  assert.equal(disabled.code,2);
  const output=JSON.parse(disabled.stdout);
  assert.ok(output.issues.includes('ENABLE_PLUGIN_IN_AGY'));
  assert.ok(output.diagnostics.some(d=>d.action.includes('agy plugin enable futureagi-tracing')));
  const disabledSetup=await f.run(['setup','antigravity','--json']);
  assert.equal(disabledSetup.code,2);
  assert.ok(JSON.parse(disabledSetup.stdout).diagnostics.some(d=>d.code==='ENABLE_PLUGIN_IN_AGY'));
  await fs.unlink(path.join(f.paths.configDir,'antigravity-registration.json'));
  for(const command of ['setup','status']) {
    const result=await f.run([command,'antigravity','--json']);
    assert.equal(result.code,2);
    assert.ok(JSON.parse(result.stdout).diagnostics.some(d=>d.code==='UNOWNED_PLUGIN_CONFLICT'));
  }
});
