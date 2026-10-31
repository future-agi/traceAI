// Execute the actual README application against loopback-only provider and OTLP endpoints.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');

(async () => {
  const packageDir = path.resolve(__dirname, '..');
  const requireFrom = createRequire(path.join(process.argv[2] || packageDir, 'package.json'));
  const readme = fs.readFileSync(path.join(packageDir, 'README.md'), 'utf8');
  const blocks = [...readme.matchAll(/```javascript\n([\s\S]*?)```/g)].map(match => match[1]);
  const streaming = process.argv[3] === 'stream';
  const code = streaming ? blocks[0].replace(
    'const person = await client.chat.completions.create(request);\n    console.log(person.name, person.age);', blocks[1]
  ) : blocks[0];
  assert(code && (!streaming || code.includes('for await')));
  const payloads = [];
  let calls = 0;
  const server = http.createServer(async (req, res) => {
    const buffers = [];
    for await (const chunk of req) buffers.push(chunk);
    const body = JSON.parse(Buffer.concat(buffers).toString());
    if (req.url === '/tracer/v1/traces') {
      payloads.push(body);
      res.writeHead(200, { 'content-type': 'application/json' }).end('{}');
    } else if (req.url === '/v1/chat/completions') {
      calls++;
      const tool = { id: 'offline-tool', type: 'function', function: { name: 'Person', arguments: '{"name":"Ada","age":36}' } };
      if (body.stream) {
        const chunk = { id: 'offline', object: 'chat.completion.chunk', created: 0, model: body.model,
          choices: [{ index: 0, delta: { tool_calls: [{ index: 0, ...tool }] }, finish_reason: null }] };
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.end(`data: ${JSON.stringify(chunk)}\n\ndata: [DONE]\n\n`);
      } else {
        res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({
          id: 'offline', object: 'chat.completion', created: 0, model: body.model,
          choices: [{ index: 0, finish_reason: 'tool_calls', message: { role: 'assistant', content: null, tool_calls: [tool] } }],
          usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 },
        }));
      }
    } else { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  Object.assign(process.env, {
    FI_API_KEY: 'offline-fi-key', FI_SECRET_KEY: 'offline-fi-secret', OPENAI_API_KEY: 'offline-provider-key',
    FI_BASE_URL: endpoint, OPENAI_BASE_URL: `${endpoint}/v1`,
  });
  try {
    await vm.runInNewContext(code, { require: requireFrom, process, console: { log() {}, error: console.error } });
    assert.equal(process.exitCode || 0, 0);
    assert.equal(calls, 1);
    const spans = payloads.flatMap(p => p.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
    assert.equal(spans.length, 2, JSON.stringify(spans));
    const parent = spans.find(s => s.name === 'Instructor Extract: Person');
    const child = spans.find(s => s !== parent);
    assert(parent && child);
    assert.equal(child.parentSpanId, parent.spanId);
    assert.equal(child.traceId, parent.traceId);
    assert.equal(parent.attributes.find(a => a.key === 'fi.span.kind').value.stringValue, 'CHAIN');
    assert.equal(child.attributes.find(a => a.key === 'fi.span.kind').value.stringValue, 'LLM');
    const wire = JSON.stringify(payloads);
    for (const hidden of ['Ada', 'offline-fi-key', 'offline-fi-secret', 'offline-provider-key']) assert(!wire.includes(hidden), hidden);
    console.log(`README ${streaming ? 'stream' : 'request'}: one extraction + one provider span; OTLP parenting and privacy passed`);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
