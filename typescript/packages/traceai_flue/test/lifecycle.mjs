import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { recorder, waitFor } from './helpers.mjs';

const fixture = fileURLToPath(new URL('./fixture', import.meta.url));
const cli = fileURLToPath(new URL('./bin/flue.mjs', import.meta.resolve('@flue/cli/package.json')));
const vite = fileURLToPath(new URL('../../bin/vite.js', import.meta.resolve('vite')));

function launch(args, env) {
  const child = spawn(process.execPath, args, { cwd: fixture, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  child.stdout.on('data', (data) => { output += data; });
  child.stderr.on('data', (data) => { output += data; });
  const timeout = setTimeout(() => child.kill('SIGKILL'), 60000);
  const done = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => { clearTimeout(timeout); resolve({ code, output }); });
  });
  return { child, done, output: () => output };
}
async function run(args, env) {
  const result = await launch(args, env).done;
  assert.equal(result.code, 0, result.output);
  return result.output;
}

async function mockModel() {
  const server = createServer(async (req, res) => {
    for await (const _ of req) { /* consume the synthetic request */ }
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    const base = { id: 'local-completion', object: 'chat.completion.chunk', created: 1700000000, model: 'local-model' };
    for (const payload of [
      { ...base, choices: [{ index: 0, delta: { role: 'assistant', content: 'PRIVATE_ANSWER' }, finish_reason: null }] },
      { ...base, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
      { ...base, choices: [], usage: { prompt_tokens: 11, completion_tokens: 3, total_tokens: 14 } },
    ]) res.write(`data: ${JSON.stringify(payload)}\n\n`);
    res.end('data: [DONE]\n\n');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { server, url: `http://127.0.0.1:${server.address().port}/v1` };
}

function checkWire(sink) {
  const spans = sink.requests.flatMap((r) => r.spans);
  const roots = spans.filter((s) => s.name === 'invoke_agent Weather');
  const models = spans.filter((s) => s.name === 'chat local-model');
  assert.equal(roots.length, 1, JSON.stringify(spans.map((s) => s.name)));
  assert.equal(models.length, 1);
  assert.equal(models[0].parentSpanId, roots[0].spanId);
  assert.equal(models[0].attributes['fi.span.kind'], 'LLM');
  assert.equal(roots[0].attributes['fi.span.kind'], 'AGENT');
  assert.equal(models[0].attributes['gen_ai.usage.input_tokens'], 11);
  assert.equal(models[0].attributes['gen_ai.usage.output_tokens'], 3);
  assert.ok(roots[0].attributes['session.id']);
  for (const request of sink.requests) {
    assert.equal(request.url, '/tracer/v1/traces');
    assert.doesNotMatch(request.body, /PRIVATE_QUESTION|PRIVATE_ANSWER|not-a-real-key/);
  }
}

test('real CLI, standalone and generated Node server deliver native spans', { timeout: 180000 }, async (t) => {
  const home = await mkdtemp(join(tmpdir(), 'traceai-flue-home-'));
  const sink = await recorder();
  const model = await mockModel();
  const env = {
    PATH: process.env.PATH ?? '', HOME: home, TMPDIR: home,
    FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', FI_PROJECT_NAME: 'lifecycle',
    FI_BASE_URL: sink.url, FI_CAPTURE_CONTENT: '0', MODEL_BASE_URL: model.url,
    OTEL_BSP_SCHEDULE_DELAY: '600000', NO_COLOR: '1',
  };
  try {
    await t.test('flue run disposes its module-scope instrumentation', async () => {
      sink.requests.length = 0;
      const output = await run([cli, 'run', 'src/agents/weather.ts', '--json', '--message', 'PRIVATE_QUESTION'], env);
      assert.match(output, /PRIVATE_ANSWER/);
      checkWire(sink);
    });
    for (const explicit of ['0', '1']) await t.test(`standalone shutdown explicit=${explicit}`, async () => {
      sink.requests.length = 0;
      assert.match(await run(['ask.ts'], { ...env, EXPLICIT_SHUTDOWN: explicit }), /PRIVATE_ANSWER/);
      checkWire(sink);
    });
    await t.test('generated server exports settled work before termination', async () => {
      await run([vite, 'build'], env);
      const probe = createServer();
      await new Promise((resolve) => probe.listen(0, '127.0.0.1', resolve));
      const port = probe.address().port;
      await new Promise((resolve) => probe.close(resolve));
      sink.requests.length = 0;
      const server = launch(['dist/server.mjs'], { ...env, PORT: String(port), HOST: '127.0.0.1' });
      try {
        await waitFor(() => /listening|localhost|127\.0\.0\.1/i.test(server.output()));
        const response = await fetch(`http://127.0.0.1:${port}/agents/weather/server`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ kind: 'user', body: 'PRIVATE_QUESTION' }),
        });
        assert.equal(response.status, 202, await response.clone().text());
        await response.text();
        await waitFor(() => sink.requests.flatMap((r) => r.spans).some((s) => s.name === 'invoke_agent Weather'));
        checkWire(sink);
        // This is not a claim that a SIGTERM immediately after settlement
        // drains a generated server: upstream calls process.exit().
      } finally { server.child.kill('SIGTERM'); await server.done; }
    });
  } finally {
    await sink.close(); model.server.closeAllConnections();
    await new Promise((resolve) => model.server.close(resolve));
    await rm(home, { recursive: true, force: true });
  }
});
