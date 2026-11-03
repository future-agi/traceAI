const assert = require('node:assert/strict');
const { readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { createServer } = require('node:http');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { test } = require('node:test');

test('Perplexity README runs unchanged with the packed ESM package', async () => {
  const consumer = process.env.OPENAI_CONSUMER_DIR;
  const readme = readFileSync(resolve(__dirname, '../README.md'), 'utf8');
  const snippet = readme.split('## Perplexity\n')[1].split('```typescript\n')[1].split('```')[0];
  const script = resolve(consumer, 'perplexity.mts');
  const preload = resolve(consumer, 'synthetic-transport.mjs');
  writeFileSync(script, snippet);
  // Only the provider transport is replaced; register() exports real OTLP to loopback.
  writeFileSync(preload, `globalThis.fetch = async (url, options) => {
    if (String(url) !== 'https://api.perplexity.ai/v1/responses') throw new Error('Unexpected network request');
    const params = JSON.parse(options.body);
    if (params.model !== 'synthetic-model') throw new Error('Missing requested model');
    return new Response(JSON.stringify({ id: 'resp-recipe', object: 'response', status: 'completed', model: 'synthetic-model', output: [{ id: 'msg-recipe', type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Synthetic answer', annotations: [] }] }], usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 } }), { headers: { 'content-type': 'application/json' } });
  };`);
  const batches = [];
  const server = createServer(async (request, response) => {
    assert.equal(request.url, '/tracer/v1/traces');
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    batches.push(JSON.parse(Buffer.concat(chunks)));
    response.writeHead(200, { 'content-type': 'application/json' }).end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const child = spawn(process.execPath, ['--import', preload, script], { env: {
      PATH: process.env.PATH,
      FI_BASE_URL: `http://127.0.0.1:${server.address().port}`,
      FI_API_KEY: 'synthetic-fi-key', FI_SECRET_KEY: 'synthetic-fi-secret',
      PERPLEXITY_API_KEY: 'synthetic-pplx-key', PERPLEXITY_MODEL: 'synthetic-model',
    }, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '', errors = '';
    child.stdout.on('data', chunk => { output += chunk; });
    child.stderr.on('data', chunk => { errors += chunk; });
    const [code] = await once(child, 'close');
    assert.equal(code, 0, errors);
    assert.match(output, /Synthetic answer/);
    const spans = batches.flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
    assert.equal(spans.length, 1);
    const attrs = Object.fromEntries(spans[0].attributes.map(a => [a.key, Object.values(a.value)[0]]));
    assert.equal(attrs['gen_ai.provider.name'], 'perplexity');
    assert.equal(String(attrs['gen_ai.usage.input_tokens']), '3');
    assert.equal(String(attrs['gen_ai.usage.output_tokens']), '2');
    assert.doesNotMatch(JSON.stringify(batches), /Synthetic answer|REST and GraphQL|synthetic-fi-key|synthetic-fi-secret|synthetic-pplx-key/);
  } finally { await new Promise(resolve => server.close(resolve)); }
});
