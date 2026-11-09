const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createServer } = require('node:http');
const cli = path.resolve(__dirname, '../dist/cli.js');

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'opencode-project-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  const project = path.join(home, 'project'), bin = path.join(home, 'bin');
  await fs.mkdir(project); await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, 'opencode'), '#!/bin/sh\nprintf "1.19.0\\n"\n', { mode: 0o755 });
  const bodies=[];
  const server = createServer((req, res) => { const chunks=[]; req.on('data',b=>chunks.push(b)); req.on('end', () => { bodies.push(JSON.parse(Buffer.concat(chunks))); res.setHeader('content-type', 'application/json'); res.end('{}'); }); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const env = { HOME: home, PATH: `${bin}:${process.env.PATH}`, XDG_CONFIG_HOME: path.join(home, '.config'), XDG_STATE_HOME: path.join(home, '.state') };
  const credentials = { FI_API_KEY: 'synthetic-public', FI_SECRET_KEY: 'synthetic-secret', FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` };
  async function run(args, vars = {}, cwd = project) {
    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [cli, ...args, '--json'], { cwd, env: { ...env, ...vars }, stdio: ['ignore', 'pipe', 'pipe'] });
      let stdout = '', stderr = '';
      child.stdout.on('data', x => stdout += x); child.stderr.on('data', x => stderr += x);
      child.on('error', reject); child.on('close', code => resolve({ code, data: JSON.parse(stdout), stderr }));
    });
  }
  return { home, project, credentials, run, bodies };
}

test('cross-agent uninstall preserves surviving project route and content opt-out', async t=>{
 const f=await fixture(t);
 for(const args of [
  ['setup','openclaw','--scope','user','--project','openclaw-global'],
  ['setup','goose','--scope','project','--project','goose'],
  ['setup','openclaw','--scope','project','--project','openclaw-private','--no-capture-content'],
  ['setup','goose','--scope','project','--project','goose'],
 ]) {
  const result=await f.run([...args,'--configure-only'],f.credentials); assert.equal(result.code,0,JSON.stringify(result));
 }
 assert.equal((await f.run(['uninstall','goose'])).code,0);
 const result=await f.run(['status','openclaw']);
 assert.equal(result.data.projectName,'openclaw-private'); assert.equal(result.data.privacy,'structure-only');
 const saved=JSON.parse(await fs.readFile(path.join(f.project,'.futureagi/agents.json'),'utf8'));
 assert.ok(saved.openclaw); assert.equal(saved.goose,undefined);
 const core=require('@traceai/agent-core');
 const config=await core.resolveConfig({agent:'openclaw',home:f.home,projectDir:f.project,env:{XDG_CONFIG_HOME:path.join(f.home,'.config'),XDG_STATE_HOME:path.join(f.home,'.state')}});
 const tracer=core.createTurnTracer({config});
 tracer.startTurn({sessionId:'survivor',turnId:'ordinary',input:'SYNTH_PRIVATE_PROMPT'}).end({output:'SYNTH_PRIVATE_RESULT'});
 await tracer.shutdown();
 assert.ok(f.bodies.some(b=>b.resourceSpans.length));
 assert.ok(!JSON.stringify(f.bodies).includes('SYNTH_PRIVATE_'));

});
