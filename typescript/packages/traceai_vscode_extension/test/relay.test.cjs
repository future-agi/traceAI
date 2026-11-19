const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const zlib = require('node:zlib');
const { setTimeout: delay } = require('node:timers/promises');
const { startRelay, encode, decode, sanitize } = require('../dist');
const token = 'a'.repeat(64);
const attr = (key, value) => ({ key, value: { stringValue: value } });
const sample = () => ({ resourceSpans: [{ resource: { attributes: [attr('service.name', 'copilot'), attr('process.cwd', '/test/project')] }, scopeSpans: [{ scope: { name: 'github.copilot', version: '1.0.100-1' }, spans: [{
  traceId: '12345678901234567890123456789012ab', spanId: '12345678901234ab', name: 'chat synthetic-model', kind: 1,
  startTimeUnixNano: '1234567890000000000', endTimeUnixNano: '1234567890100000000',
  attributes: [attr('gen_ai.operation.name', 'chat'), attr('gen_ai.request.model', 'synthetic-model'),
    { key: 'gen_ai.usage.input_tokens', value: { intValue: '42' } }, attr('gen_ai.input.messages', 'private-prompt'), attr('gen_ai.output.messages', 'private-output'),
    attr('gen_ai.tool.call.arguments', 'private-tool-input'), attr('gen_ai.tool.call.result', 'private-tool-output'), attr('arbitrary.content', 'private-arbitrary')],
  events: [{ name: 'private-event', attributes: [attr('body', 'private-event-body')] }], status: { code: 2, message: 'private-error' }
}] }] }] });
async function waitFor(fn) { for (let i=0; i<300; i++) { if (fn()) return; await delay(10); } assert.fail('Timed out awaiting relay result'); }
async function fixture(t, override = {}, downstream) {
  const received = [];
  const dest = http.createServer(async (req, res) => {
    const chunks = []; for await (const c of req) chunks.push(c);
    received.push({ headers: req.headers, body: Buffer.concat(chunks) });
    if (downstream) return downstream(req, res);
    res.setHeader('Content-Type', req.headers['content-type']);
    res.end(req.headers['content-type'] === 'application/x-protobuf' ? Buffer.alloc(0) : '{}');
  });
  await new Promise(r => dest.listen(0, '127.0.0.1', r));
  t.after(() => new Promise(r => { dest.closeAllConnections(); dest.close(r); }));
  const config = { enabled: true, destinationApproved: true, captureContent: true, hideInputs: false, hideOutputs: false, hideErrorDetails: false,
    credentials: { apiKey: 'synthetic-api', secretKey: 'synthetic-secret' }, endpoint: `http://127.0.0.1:${dest.address().port}/tracer/v1/traces`, projectName: 'synthetic-project' };
  const relay = await startRelay({ port: 0, token, config: async () => config, ...override });
  t.after(() => relay.stop());
  function post(body = JSON.stringify(sample()), options = {}) {
    return new Promise((resolve, reject) => {
      const req = http.request({ hostname: '127.0.0.1', port: relay.port, method: options.method ?? 'POST', path: options.path ?? `/${token}/v1/traces`,
        headers: { 'Content-Type': 'application/json', ...options.headers } }, res => { res.resume(); res.on('end', () => resolve(res.statusCode)); });
      req.on('error', reject); req.end(body);
    });
  }
  return { received, config, relay, post };
}
test('forwards JSON to exact destination with injected credentials, not inbound headers', async t => {
  const f = await fixture(t);
  assert.equal(await f.post(undefined, { headers: { 'X-Api-Key': 'untrusted', 'X-Project-Name': 'untrusted' } }), 200);
  await waitFor(() => f.relay.state.exported === 1);
  const r = f.received[0];
  assert.equal(r.headers['x-api-key'], 'synthetic-api'); assert.equal(r.headers['x-secret-key'], 'synthetic-secret'); assert.equal(r.headers['x-project-name'], 'synthetic-project');
  const span = JSON.parse(r.body).resourceSpans[0].scopeSpans[0].spans[0];
  assert.equal(span.traceId, sample().resourceSpans[0].scopeSpans[0].spans[0].traceId);
  assert.ok(span.attributes.some(a => a.key === 'gen_ai.input.messages'));
});
for (const variant of ['path', 'host', 'origin', 'empty-origin', 'referer', 'method', 'query', 'unicode']) test(`rejects ${variant} at ingress`, async t => {
  const f = await fixture(t);
  const opts = { path: '/v1/traces' };
  if (variant === 'host') Object.assign(opts, { path: `/${token}/v1/traces`, headers: { Host: 'attacker.invalid' } });
  if (variant.includes('origin')) Object.assign(opts, { path: `/${token}/v1/traces`, headers: { Origin: variant === 'empty-origin' ? '' : 'http://127.0.0.1' } });
  if (variant === 'referer') Object.assign(opts, { path: `/${token}/v1/traces`, headers: { Referer: 'http://localhost' } });
  if (variant === 'method') Object.assign(opts, { path: `/${token}/v1/traces`, method: 'GET' });
  if (variant === 'query') opts.path = `/${token}/v1/traces?x=1`;
  if (variant === 'unicode') opts.path = `/${'é'.repeat(64)}/v1/traces`;
  const status = await f.post(undefined, opts);
  assert.ok(status === 403 || variant === 'unicode' && status === 400); // Node's HTTP parser can reject raw non-ASCII first.
  assert.equal(f.received.length, 0);
});
for (const flag of ['captureContent', 'hideInputs', 'hideOutputs', 'hideErrorDetails']) test(`${flag}: removes content from attributes, events, resources and status`, async t => {
  const f = await fixture(t); f.config[flag] = flag !== 'captureContent';
  assert.equal(await f.post(), 200); await waitFor(() => f.relay.state.exported === 1);
  assert.doesNotMatch(f.received[0].body.toString(), /private-|\/test\/project/);
  assert.match(f.received[0].body.toString(), /input_tokens/);
});
test('gzip protobuf roundtrip preserves exact int64 timestamps and native IDs', async t => {
  const f = await fixture(t); f.config.hideInputs = true;
  const raw = sample(), span = raw.resourceSpans[0].scopeSpans[0].spans[0];
  span.traceId = Buffer.from(span.traceId, 'hex'); span.spanId = Buffer.from(span.spanId, 'hex');
  assert.equal(await f.post(zlib.gzipSync(encode(raw, true)), { headers: { 'Content-Type': 'application/x-protobuf', 'Content-Encoding': 'gzip' } }), 200);
  await waitFor(() => f.relay.state.exported === 1);
  const out = decode(f.received[0].body, true).resourceSpans[0].scopeSpans[0].spans[0];
  assert.deepEqual(out.traceId, span.traceId); assert.equal(out.startTimeUnixNano, span.startTimeUnixNano);
  assert.doesNotMatch(JSON.stringify(out), /private-/);
});
for (const protobuf of [false, true]) for (const captureContent of [true, false]) {
  test(`${protobuf ? 'protobuf' : 'JSON'} relay scrubs credential aliases in every attribute list (content=${captureContent})`, async t => {
    const f = await fixture(t); f.config.captureContent = captureContent;
    const raw = sample(), resource = raw.resourceSpans[0], scope = resource.scopeSpans[0], span = scope.spans[0];
    if (protobuf) {
      span.traceId = Buffer.from(span.traceId, 'hex'); span.spanId = Buffer.from(span.spanId, 'hex');
    }
    const aliases = ['access_token', 'refresh_token', 'private_key', 'passwd', 'Access-Token', 'refreshToken', 'PRIVATE.KEY', 'pwd', 'signing_key', 'credentials'];
    const counters = [
      ['gen_ai.usage.input_tokens', { intValue: '42' }],
      ['gen_ai.usage.output_tokens', { intValue: '7' }],
      ['gen_ai.usage.total_tokens', { intValue: '49' }],
      ['gen_ai.usage.cache_read_tokens', { intValue: '5' }],
      ['gen_ai.usage.cache_write_tokens', { intValue: '3' }],
      ['gen_ai.usage.output_tokens.reasoning', { intValue: '2' }],
      ['gen_ai.usage.cache_read.input_tokens', { intValue: '5' }],
      ['gen_ai.usage.cache_write.input_tokens', { intValue: '3' }],
      ['gen_ai.usage.reasoning.output_tokens', { intValue: '2' }],
      ['llm.token_count.prompt', { intValue: '42' }],
      ['gen_ai.server.time_to_first_token', { doubleValue: 0.125 }],
    ];
    const attributes = () => [
      attr('service.name', 'visible-service'),
      ...aliases.map((key, i) => attr(key, `SYNTHETIC_ALIAS_CANARY_${i}`)),
      ...counters.map(([key, value]) => ({ key, value })),
      // Neither arbitrary token-prefixed names nor non-numeric counter values qualify.
      attr('gen_ai.usage.input_tokens', 'SYNTHETIC_USAGE_CANARY'),
      attr('gen_ai.usage.access_token', 'SYNTHETIC_USAGE_CANARY'),
      { key: 'gen_ai.usage.access_token', value: { intValue: '123456' } },
      { key: 'gen_ai.usage.total_tokens', value: { arrayValue: { values: [{ stringValue: 'SYNTHETIC_USAGE_CANARY' }] } } },
      { key: 'nested', value: { kvlistValue: { values: aliases.map(key => attr(key, 'SYNTHETIC_NESTED_CANARY')) } } },
    ];
    resource.resource.attributes = attributes(); scope.scope.attributes = attributes(); span.attributes = attributes();
    span.events = [{ name: 'event', attributes: attributes() }];
    span.links = [{ traceId: span.traceId, spanId: span.spanId, attributes: attributes() }];
    assert.equal(await f.post(encode(raw, protobuf), { headers: { 'Content-Type': protobuf ? 'application/x-protobuf' : 'application/json' } }), 200);
    await waitFor(() => f.relay.state.exported === 1);
    const out = decode(f.received[0].body, protobuf), r = out.resourceSpans[0], s = r.scopeSpans[0], p = s.spans[0];
    assert.doesNotMatch(JSON.stringify(out), /SYNTHETIC_.*CANARY/);
    const lists = [r.resource.attributes, s.scope.attributes, p.attributes, p.links[0].attributes];
    if (captureContent) lists.push(p.events[0].attributes);
    else assert.equal(p.events?.length ?? 0, 0);
    for (const list of lists) {
      for (const key of aliases) assert.equal(list.some(a => a.key === key), false, key);
      assert.equal(list.some(a => a.key === 'gen_ai.usage.access_token'), false);
      assert.equal(list.find(a => a.key === 'service.name').value.stringValue, 'visible-service');
      for (const [key, value] of counters) {
        const matches = list.filter(a => a.key === key);
        assert.equal(matches.length, 1, key);
        assert.deepEqual(matches[0].value, value, key);
      }
    }
  });
}

test('compressed and decompressed bodies are bounded', async t => {
  const f = await fixture(t, { maxBytes: 2048 });
  assert.equal(await f.post('x'.repeat(2049), { headers: { 'Content-Length': '2049' } }), 413);
  assert.equal(await f.post(zlib.gzipSync('x'.repeat(5000)), { headers: { 'Content-Encoding': 'gzip' } }), 400);
  assert.equal(f.received.length, 0);
});
test('disabled, revoked and missing credentials never export', async t => {
  const f = await fixture(t); f.config.enabled = false;
  assert.equal(await f.post(), 200); await waitFor(() => f.relay.state.dropped === 1);
  f.config.enabled = true; f.config.revalidate = async () => false;
  assert.equal(await f.post(), 200); await waitFor(() => f.relay.state.dropped === 2);
  delete f.config.revalidate; delete f.config.credentials;
  assert.equal(await f.post(), 200); await waitFor(() => f.relay.state.dropped === 3);
  assert.equal(f.received.length, 0);
});
test('malformed batches and destination failures remain fail-open with static diagnostics', async t => {
  const f = await fixture(t, {}, (_req, res) => { res.writeHead(503); res.end('synthetic-sensitive-server-body'); });
  assert.equal(await f.post('{broken'), 200); await waitFor(() => f.relay.state.dropped === 1);
  assert.equal(await f.post(), 200); await waitFor(() => f.relay.state.dropped === 2);
  assert.doesNotMatch(JSON.stringify(f.relay.state), /sensitive|private-/);
});
test('queue admission is bounded and slow destination has a deadline', async t => {
  const f = await fixture(t, { maxQueueBytes: 2048, timeoutMs: 60 }, () => {});
  await Promise.all(Array.from({ length: 20 }, () => f.post()));
  await waitFor(() => f.relay.state.dropped > 0);
  assert.ok(f.relay.state.received < 20); assert.ok(f.received.length < 20);
});
test('project scope fails closed without unambiguous native workspace attribution', async t => {
  const f = await fixture(t, { projectDir: '/test/project' });
  const missing = sample(); missing.resourceSpans[0].resource.attributes = [];
  assert.equal(await f.post(JSON.stringify(missing)), 200); await waitFor(() => f.relay.state.lastError === 'WORKSPACE_ATTRIBUTION_REQUIRED');
  assert.equal(f.received.length, 0);
  assert.equal(await f.post(), 200); await waitFor(() => f.relay.state.exported === 1);
});
test('JSON enum names retain error outcomes', () => {
  const raw = sample(); raw.resourceSpans[0].scopeSpans[0].spans[0].status.code = 'STATUS_CODE_ERROR';
  const out = sanitize(raw, { captureContent: false }).body.resourceSpans[0].scopeSpans[0].spans[0];
  assert.equal(out.status.code, 2); assert.equal(out.status.message, undefined);
});
test('policy changes fence queued batches before the next export', async t => {
  let release;
  const f = await fixture(t, {}, (_req, res) => {
    release = () => { res.setHeader('content-type', 'application/json'); res.end('{}'); };
  });
  await f.post(); await waitFor(() => !!release);
  await f.post();
  f.config.enabled = false;
  release();
  await waitFor(() => f.relay.state.dropped > 0);
  assert.equal(f.received.length, 1);
});
test('content-on redacts embedded credentials and never forwards arbitrary OTLP extensions', () => {
  const raw = sample(); raw.resourceSpans[0].scopeSpans[0].spans[0].attributes.push(attr('input.value', '{"api_key":"synthetic-secret"}'));
  raw.resourceSpans[0].scopeSpans[0].spans[0].attributes.push({ key: 'gen_ai.tool.call.arguments', value: { kvlistValue: { values: [
    { key: 'query', value: { stringValue: 'structured-input' } }, { key: 'api_key', value: { stringValue: 'synthetic-secret' } }
  ] } } });
  raw.resourceSpans[0].scopeSpans[0].spans[0].unknownField = 'private-extension';
  const result = JSON.stringify(sanitize(raw, { captureContent: true }).body);
  assert.doesNotMatch(result, /synthetic-secret|private-extension/);
  assert.match(result, /structured-input/);
});
