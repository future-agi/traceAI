const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {join} = require('node:path');
const {tmpdir} = require('node:os');
const {resolveConfig} = require('@traceai/agent-core');
const {createOpenClawAdapter,parseHost} = require('../dist/adapter.js');
const version=require('../package.json').version;

test('update stages the new owned plugin and requires fresh native consent before replacement',async t=>{
  const home=await fs.mkdtemp(join(tmpdir(),'fi-upgrade-'));
  t.after(()=>fs.rm(home,{recursive:true,force:true}));
  const host=join(home,'.openclaw');await fs.mkdir(host,{mode:0o700});
  const file=join(host,'openclaw.json');
  const original='// keep my comment\n{plugins:{allow:["other","diagnostics-otel","futureagi-openclaw"]},unrelated:{flag:true}}';
  await fs.writeFile(file,original,{mode:0o600});
  const config=await resolveConfig({agent:'openclaw',home,env:{}});
  let lifecycleVersion=version, packageName='@traceai/futureagi-openclaw';
  const commands=[];
  const adapter=createOpenClawAdapter(async args=>{
    commands.push(args);
    if(args[0]==='--version')return 'OpenClaw 2026.10.5';
    assert.equal(args[1],'inspect','headless mode never performs replacement or accepts trust');
    return JSON.stringify({plugin:{enabled:true,status:'loaded',trustedOfficialInstall:true,
      packageName:args[2]==='diagnostics-otel'?'@openclaw/diagnostics-otel':packageName,
      version:args[2]==='diagnostics-otel'?'2026.10.5':lifecycleVersion}});
  });
  const context={config,scope:'user'};
  assert.equal((await adapter.install(context)).registered,true);
  const owned=await fs.readFile(file,'utf8');
  lifecycleVersion='0.0.9';
  assert.equal((await adapter.inspect(context)).registered,false);
  const update=await adapter.install(context);
  assert.equal(update.registered,false);assert.equal(update.trust,'required');
  assert.equal(update.detail,'OPENCLAW_LIFECYCLE_REVIEW_REQUIRED');
  assert.match(update.recovery,/plugins uninstall futureagi-openclaw/);
  assert.match(update.recovery,/plugins install/);
  assert.doesNotMatch(update.recovery,/--force|--accept-capabilities/);
  assert.equal(await fs.readFile(file,'utf8'),owned,'pending consent does not modify host settings');
  const staged=join(config.paths.configDir,'openclaw-lifecycle',version);
  assert.equal(JSON.parse(await fs.readFile(join(staged,'package.json'),'utf8')).version,version);
  lifecycleVersion=version; // native user-approved replacement completed
  assert.equal((await adapter.install(context)).registered,true);
  assert.equal(parseHost(await fs.readFile(file,'utf8')).unrelated.flag,true);
  assert.match(await fs.readFile(file,'utf8'),/keep my comment/);
  packageName='another-package';
  await assert.rejects(adapter.install(context),/LIFECYCLE_REQUIRES_ATTENTION/);
});
