// Run only inside E2B after installing locally packed distributions into an empty directory.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { createRequire } = require('node:module');
const { once } = require('node:events');
async function main() {
  const [consumer, samplePath] = process.argv.slice(2);
  const req = createRequire(path.join(consumer, 'package.json'));
  const core = req('@traceai/agent-core');
  const entry = req.resolve('@traceai/devin-cascade');
  const registration = req(path.join(path.dirname(entry), 'registration.js'));
  const adapter = req('@traceai/devin-cascade').cascadeAdapter;
  const home = path.join(consumer, 'isolated-home'); await fs.mkdir(home, { mode: 0o700 });
  const captured = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', b => chunks.push(b));
    request.on('end', () => { captured.push(JSON.parse(Buffer.concat(chunks))); response.setHeader('content-type', 'application/json'); response.end('{}'); });
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  try {
    const options = { agent: 'devin-cascade', home, env: {} };
    await core.setup({ ...options, configureOnly: true, projectName: 'packed-cascade',
      credentials: { apiKey: 'synthetic-key', secretKey: 'synthetic-secret' },
      otlpEndpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces` });
    const config = await core.resolveConfig(options), context = { config, scope: 'user' };
    // Exercise only the low-level contract: the public adapter remains qualification-gated.
    assert.equal((await adapter.detect(context)).supported, false);
    assert.equal((await registration.installHooks(context, path.join(home, 'runtime'))).registered, true);
    const hooks = JSON.parse(await fs.readFile(await registration.hookTarget(context), 'utf8')).hooks;
    const transcript = path.join(home, '.windsurf/transcripts/packed-session.jsonl');
    await fs.mkdir(path.dirname(transcript), { recursive: true, mode: 0o700 });
    const invoke = async action => {
      const child = spawn('/bin/bash', ['-c', hooks[action][0].command], { cwd: home, env: { HOME: home, PATH: '/empty' } });
      let stdout = '', stderr = ''; child.stdout.on('data', b => stdout += b); child.stderr.on('data', b => stderr += b);
      child.stdin.end(JSON.stringify({ agent_action_name: action, trajectory_id: 'packed-session', execution_id: 'packed-turn',
        tool_info: action === 'pre_user_prompt' ? { user_prompt: 'create a hello world file' } : { transcript_path: transcript } }));
      const [code] = await once(child, 'close'); assert.equal(code, 0); assert.equal(stdout, ''); assert.equal(stderr, '');
    };
    await invoke('pre_user_prompt'); await fs.writeFile(transcript, await fs.readFile(samplePath), { mode: 0o600 });
    await invoke('post_cascade_response_with_transcript');
    const spans = captured.flatMap(r => r.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(s => s.spans)));
    assert.equal(spans.length, 4); assert.equal(spans.filter(s => !s.parentSpanId).length, 1);
    await core.disable(options); await invoke('post_cascade_response_with_transcript');
    assert.equal(captured.filter(r => r.resourceSpans.length).length, 1);
    const result = { kind: 'PACKED_ADAPTER_FIXTURE_CONTRACT_NOT_REAL_AGENT', spans: 4, roots: 1, reconstructedResponses: 2,
      tools: 1, generations: 2, nativeRequests: 0, usageCounters: 0, silent: true, disabledExportPrevented: true, guiQualified: false };
    await fs.writeFile(path.join(consumer, 'capture.json'), JSON.stringify({ result, captured }, null, 2));
    console.log(JSON.stringify(result));
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)); }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
