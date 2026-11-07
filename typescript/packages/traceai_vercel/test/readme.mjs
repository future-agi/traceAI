// Execute the README's application code in a packed consumer. The only source
// substitution is the OTLP URL; provider fetch is synthetic and external network
// must be denied by the caller.
import assert from 'node:assert/strict';
import { readFile, writeFile, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';

const consumer = resolve(process.argv[2]);
const source = process.argv[3] ? resolve(process.argv[3]) : new URL('../README.md', import.meta.url);
const text = await readFile(source, 'utf8');
const blocks = [...text.matchAll(/```javascript[^\n]*\n([\s\S]*?)```/g)].map(match => match[1]);
assert.equal(blocks.length, 2);
const packets = [];
const server = createServer((req, res) => {
  const chunks = [];
  req.on('data', chunk => chunks.push(chunk));
  req.on('end', () => {
    packets.push({ url: req.url, headers: req.headers, data: JSON.parse(Buffer.concat(chunks).toString()) });
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const files = ['tracing.mjs', 'generate.mjs', 'synthetic-fetch.mjs'].map(file => resolve(consumer, file));
try {
  const endpoint = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
  assert.ok(blocks[0].includes('https://api.futureagi.com/tracer/v1/traces'));
  await writeFile(files[0], blocks[0].replace('https://api.futureagi.com/tracer/v1/traces', endpoint));
  await writeFile(files[1], blocks[1]);
  await writeFile(files[2], `
    import assert from 'node:assert/strict';
    globalThis.fetch = async (url) => {
      assert.equal(String(url), 'https://api.openai.com/v1/chat/completions');
      return Response.json({ id: 'readme-test', object: 'chat.completion', created: 1, model: 'gpt-4o-mini',
        choices: [{ index: 0, message: { role: 'assistant', content: 'synthetic-book-club-names' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 } });
    };
  `);
  const child = spawn(process.execPath, ['--import', files[2], files[1]], {
    cwd: consumer,
    env: { PATH: process.env.PATH, FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret', OPENAI_API_KEY: 'synthetic-openai' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '', stderr = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  const exitCode = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  assert.equal(exitCode, 0, stderr);
  assert.equal(stdout.trim(), 'synthetic-book-club-names');
  const spans = packets.flatMap(packet => {
    assert.equal(packet.url, '/tracer/v1/traces');
    assert.equal(packet.headers['x-api-key'], 'synthetic-api');
    assert.equal(packet.headers['x-secret-key'], 'synthetic-secret');
    return packet.data.resourceSpans.flatMap(resource => {
      assert.ok(resource.resource.attributes.some(attr => attr.key === 'project_name' && attr.value.stringValue === 'vercel-app'));
      return resource.scopeSpans.flatMap(scope => scope.spans);
    });
  });
  assert.equal(spans.length, 3);
  assert.equal(spans.filter(span => span.attributes.some(attr => attr.key === 'fi.span.kind' && attr.value.stringValue === 'LLM')).length, 1);
  assert.ok(!JSON.stringify(packets).includes('neighborhood book club'));
  assert.ok(!JSON.stringify(packets).includes('synthetic-book-club-names'));
  console.log('PASS customer snippets: native registration, one LLM span, privacy, auth, resource and shutdown');
} finally {
  await Promise.all(files.map(file => rm(file, { force: true })));
  await new Promise(resolve => server.close(resolve));
}
