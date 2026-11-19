const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const core = require('@traceai/agent-core');
const {handleEvent, pendingTurns} = require('../dist/runtime');

// The timestamp is deliberately never backdated. This reproduces the fresh-lease
// SIGKILL bug: a six-second Stop cannot acquire a lease with a 30-second expiry.
test('exit observer retries a fresh crashed event lease and finalizes without another prompt', {timeout:65000}, async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'fi-agy-exit-lock-'));
  const batches = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', b => chunks.push(b));
    req.on('end', () => {
      batches.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.setHeader('Content-Type', 'application/json');
      res.end('{}');
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const children = [];
  t.after(async () => {
    for (const child of children) if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    server.closeAllConnections();
    await new Promise(r => server.close(r));
    await fs.rm(home, {recursive:true, force:true});
  });
  const options = {agent:'antigravity', home, env:{}};
  await core.setup({...options, configureOnly:true, credentials:{apiKey:'synthetic-api', secretKey:'synthetic-secret'},
    projectName:'test', otlpEndpoint:`http://127.0.0.1:${server.address().port}/tracer/v1/traces`});
  const config = await core.resolveConfig(options);
  const payload = {conversationId:'crashed-lock', initialNumSteps:1, invocationNum:0, lastUserInput:'Synthetic prompt',
    transcriptPath:path.join(home, '.gemini/antigravity-cli/brain/crashed-lock/.system_generated/logs/transcript_full.jsonl')};
  await fs.mkdir(path.dirname(payload.transcriptPath), {recursive:true, mode:0o700});
  await fs.writeFile(payload.transcriptPath, [
    {step_index:0, source:'USER_EXPLICIT', type:'USER_INPUT', status:'DONE', content:'Synthetic prompt'},
    {step_index:1, source:'MODEL', type:'PLANNER_RESPONSE', status:'DONE', content:'Synthetic answer', input_tokens:17, output_tokens:3},
  ].map(JSON.stringify).join('\n')+'\n');
  // A process identity known to be gone. The observer must match this checkpoint.
  const host = {pid:2147483647, start:'1'};
  await handleEvent('PreInvocation', payload, {...options, host});
  await handleEvent('PostInvocation', payload, options);
  const directory = path.join(config.paths.stateDir, 'antigravity-conversations');
  const name = (await fs.readdir(directory)).find(n => n.endsWith('.json'));
  const file = path.join(directory, name);
  const lockPath = file.replace('.json', '.event-lock-v2');
  const writer = spawn(process.execPath, ['-e', `require('proper-lockfile').lock(${JSON.stringify(file)}, {
    realpath:false, lockfilePath:${JSON.stringify(lockPath)}, stale:30000, update:1000
  }).then(() => {process.send('locked');setInterval(() => {},1000)}).catch(() => process.exit(1));`],
    {cwd:path.resolve(__dirname, '..'), stdio:['ignore','ignore','pipe','ipc']});
  children.push(writer);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('lock writer did not acquire')), 5000);
    writer.once('message', () => {clearTimeout(timer); resolve();});
    writer.once('error', e => {clearTimeout(timer); reject(e);});
  });
  const exited = new Promise(r => writer.once('close', r));
  writer.kill('SIGKILL');
  await exited;
  const lockTime = (await fs.stat(lockPath)).mtimeMs;
  assert.ok(Math.abs(Date.now()-lockTime) < 5000, 'the abandoned lock must still be fresh');
  const started = Date.now();
  const observer = spawn(process.execPath, [path.resolve(__dirname, '../plugin/hook.cjs'), '--recover-exit', String(host.pid), host.start], {
    env:{HOME:home, PATH:process.env.PATH}, stdio:['pipe','pipe','pipe'],
  });
  children.push(observer);
  const recovery = new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    observer.stdout.on('data', b => stdout += b);
    observer.stderr.on('data', b => stderr += b);
    observer.once('error', reject);
    observer.once('close', (code, signal) => resolve({code, signal, stdout, stderr}));
  });
  observer.stdin.end(JSON.stringify(payload));
  // The terminal hook times out neutrally. Only the observer can recover later.
  assert.equal(await handleEvent('Stop', {...payload, fullyIdle:true, terminationReason:'NO_TOOL_CALL'}, options), 'retry-lock');
  assert.equal(await pendingTurns(config), 1);
  const result = await recovery;
  assert.deepEqual(result, {code:0, signal:null, stdout:'', stderr:''});
  const spans = batches.flatMap(b => (b.resourceSpans ?? []).flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.filter(s => !s.parentSpanId).length, 1);
  assert.equal(await pendingTurns(config), 0);
  assert.ok(Date.now()-started >= 25000, 'recovery must wait for stale expiry, not steal the lease');
  await handleEvent('HostExit', payload, {...options, host});
  const after = batches.flatMap(b => (b.resourceSpans ?? []).flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(after.length, spans.length, 'durable tombstones prevent a duplicate finalization');
  console.log('CRASHED_LEASE_RECOVERED', JSON.stringify({roots:1, pending:0, elapsedMs:Date.now()-started}));
});
