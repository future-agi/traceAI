// Runs the README recipe unchanged against installed tarballs:
//   FIREWORKS_CONSUMER_DIR=/path/to/consumer node --test test/readme.cjs
const assert = require('node:assert/strict');
const { readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { createServer } = require('node:http');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { test } = require('node:test');

test('Fireworks README recipe runs with the packed packages', async () => {
  const consumer = process.env.FIREWORKS_CONSUMER_DIR;
  const readme = readFileSync(resolve(__dirname, '../README.md'), 'utf8');
  const snippet = readme.split('## Set up Fireworks tracing\n')[1].split('```typescript\n')[1].split('```')[0];
  const script = resolve(consumer, 'fireworks.mts');
  const preload = resolve(consumer, 'synthetic-transport.mjs');
  writeFileSync(script, snippet);
  // Only the model transport is replaced; register() exports real OTLP to loopback.
  writeFileSync(preload, `globalThis.fetch = async (url, options) => {
    if (String(url) !== 'https://api.fireworks.ai/inference/v1/chat/completions') throw new Error('Unexpected network request');
    if (JSON.parse(options.body).model !== 'accounts/fireworks/models/synthetic') throw new Error('Missing requested model');
    return new Response(JSON.stringify({ id: 'chat-recipe', object: 'chat.completion', created: 1, model: 'accounts/fireworks/models/synthetic', choices: [{ index: 0, message: { role: 'assistant', content: 'Synthetic answer' }, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } }), { headers: { 'content-type': 'application/json' } });
  };`);
  const batches = [];
  const server = createServer(async (request, response) => {
    assert.equal(request.url, '/tracer/v1/traces');
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    batches.push(JSON.parse(Buffer.concat(chunks)));
    response.writeHead(200, { 'content-type': 'application/json' }).end('{}');
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  try {
    const child = spawn(process.execPath, ['--import', preload, script], {
      cwd: consumer,
      env: {
        PATH: process.env.PATH,
        FI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
        FI_API_KEY: 'synthetic-fi-key',
        FI_SECRET_KEY: 'synthetic-fi-secret',
        FIREWORKS_API_KEY: 'synthetic-fireworks-key',
        FIREWORKS_MODEL: 'accounts/fireworks/models/synthetic',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    let errors = '';
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { errors += chunk; });
    const [code] = await once(child, 'close');
    assert.equal(code, 0, errors);
    assert.match(output, /Synthetic answer/);
    const spans = batches.flatMap((b) => b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)));
    assert.equal(spans.length, 1);
    assert.equal(spans[0].name, 'OpenAI Chat Completions');
    const attrs = Object.fromEntries(spans[0].attributes.map((a) => [a.key, Object.values(a.value)[0]]));
    assert.equal(attrs['gen_ai.provider.name'], 'fireworks');
    assert.equal(String(attrs['gen_ai.usage.input_tokens']), '3');
    assert.equal(String(attrs['gen_ai.usage.output_tokens']), '2');
    assert.doesNotMatch(JSON.stringify(batches), /synthetic-fi-key|synthetic-fi-secret|synthetic-fireworks-key/);
  } finally {
    await new Promise((done) => server.close(done));
  }
});
