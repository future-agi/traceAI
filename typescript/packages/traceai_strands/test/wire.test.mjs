import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { gunzipSync } from 'node:zlib';
import { once } from 'node:events';
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { require, agent, SECRET } from './fixture.mjs';
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { context, trace, propagation } = require('@opentelemetry/api');
const { StrandsSpanExporter } = process.env.STRANDS_TEST_PACKED
  ? require('@traceai/strands/native') : await import('../dist/src/native.js');

// Decode independently of the exporter. OTLP field numbers follow
// opentelemetry-proto/common/v1, resource/v1, trace/v1 and collector/trace/v1.
const proto = require('protobufjs').parse(`syntax = "proto3";
message AnyValue { oneof value { string stringValue=1; bool boolValue=2; int64 intValue=3; double doubleValue=4; ArrayValue arrayValue=5; KeyValueList kvlistValue=6; bytes bytesValue=7; } }
message ArrayValue { repeated AnyValue values=1; }
message KeyValue { string key=1; AnyValue value=2; }
message KeyValueList { repeated KeyValue values=1; }
message Resource { repeated KeyValue attributes=1; }
message Scope { string name=1; string version=2; repeated KeyValue attributes=3; }
message Event { fixed64 timeUnixNano=1; string name=2; repeated KeyValue attributes=3; }
message Status { string message=2; int32 code=3; }
message Span { bytes traceId=1; bytes spanId=2; bytes parentSpanId=4; string name=5; int32 kind=6; fixed64 startTimeUnixNano=7; fixed64 endTimeUnixNano=8; repeated KeyValue attributes=9; repeated Event events=11; Status status=15; }
message ScopeSpans { Scope scope=1; repeated Span spans=2; }
message ResourceSpans { Resource resource=1; repeated ScopeSpans scopeSpans=2; }
message ExportTraceServiceRequest { repeated ResourceSpans resourceSpans=1; }
`).root.lookupType('ExportTraceServiceRequest');
const attributes = (list = []) => Object.fromEntries(list.map(({ key, value }) => [key,
  value.stringValue ?? (value.intValue !== undefined ? Number(value.intValue) : undefined) ?? value.doubleValue ?? value.boolValue]));

async function receiver() {
  const requests = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    if (req.url === '/v1/chat/completions') {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      for (const chunk of [
        { choices: [{ index: 0, delta: { role: 'assistant', content: 'An API connects applications.' }, finish_reason: null }] },
        { choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } },
      ]) res.write(`data: ${JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk', created: 1, model: 'gpt-4o-mini', ...chunk })}\n\n`);
      res.end('data: [DONE]\n\n');
      return;
    }
    let body = Buffer.concat(chunks);
    if (req.headers['content-encoding'] === 'gzip') body = gunzipSync(body);
    const protobuf = req.headers['content-type']?.includes('protobuf');
    const decoded = protobuf ? proto.toObject(proto.decode(body), { longs: String, bytes: String }) : JSON.parse(body);
    requests.push({ headers: req.headers, path: req.url, decoded });
    res.writeHead(200, { 'content-type': protobuf ? 'application/x-protobuf' : 'application/json' });
    res.end(protobuf ? Buffer.alloc(0) : '{}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { server, requests, url: `http://127.0.0.1:${server.address().port}` };
}
function flatten(requests) {
  return requests.flatMap(({ decoded }) => decoded.resourceSpans.flatMap((resource) => resource.scopeSpans.flatMap((scope) => scope.spans)));
}
function assertHeaders(requests) {
  assert.ok(requests.length > 0);
  for (const request of requests) {
    assert.equal(request.path, '/tracer/v1/traces');
    assert.equal(request.headers['x-api-key'], 'synthetic-api');
    assert.equal(request.headers['x-secret-key'], 'synthetic-secret');
    for (const resource of request.decoded.resourceSpans) {
      assert.equal(attributes(resource.resource.attributes).project_name, 'strands-app');
      assert.equal(attributes(resource.resource.attributes).project_type, 'observe');
    }
  }
}

for (const protocol of ['http', 'proto']) {
  for (const compression of ['none', 'gzip']) {
    for (const captureContent of [false, true]) {
      test(`OTLP ${protocol} ${compression} content=${captureContent}`, async () => {
        context.disable(); trace.disable(); propagation.disable();
        const { server, requests, url } = await receiver();
        const { OTLPTraceExporter } = require(`@opentelemetry/exporter-trace-otlp-${protocol}`);
        const raw = new InMemorySpanExporter();
        const provider = new NodeTracerProvider({
          resource: resourceFromAttributes({ project_name: 'strands-app', project_type: 'observe' }),
          spanProcessors: [new SimpleSpanProcessor(new StrandsSpanExporter(new OTLPTraceExporter({
            url: `${url}/tracer/v1/traces`, compression,
            headers: { 'x-api-key': 'synthetic-api', 'x-secret-key': 'synthetic-secret' },
          }), { captureContent, modelProvider: 'openai' })), new SimpleSpanProcessor(raw)],
        });
        provider.register();
        try {
          await agent('wire-tools', 'tool').invoke(SECRET);
          await assert.rejects(agent('wire-failure', 'error').invoke(SECRET));
          await provider.forceFlush();
          assertHeaders(requests);
          assert.ok(requests.every((r) => (r.headers['content-encoding'] === 'gzip') === (compression === 'gzip')));
          const list = flatten(requests);
          assert.equal(list.length, raw.getFinishedSpans().length, 'no duplicate or missing spans on the wire');
          const ids = new Map(list.map((s) => [s.spanId, s]));
          assert.equal(ids.size, list.length);
          for (const span of list) {
            if (span.parentSpanId) {
              assert.ok(ids.has(span.parentSpanId), `missing parent for ${span.name}`);
              assert.equal(ids.get(span.parentSpanId).traceId, span.traceId);
            }
            assert.ok(attributes(span.attributes)['fi.span.kind']);
            assert.ok(!span.status?.message, 'exception text is separately opt-in');
            assert.ok(!(span.events ?? []).some((e) => e.name === 'exception'));
          }
          assert.equal(list.reduce((sum, s) => sum + (attributes(s.attributes)['gen_ai.usage.total_tokens'] ?? 0), 0), 10);
          assert.equal(JSON.stringify(requests).includes(SECRET), captureContent);
          if (captureContent) {
            const model = list.find((s) => attributes(s.attributes)['fi.span.kind'] === 'LLM');
            assert.equal(attributes(model.attributes)['gen_ai.provider.name'], 'openai');
            assert.doesNotThrow(() => JSON.parse(attributes(model.attributes)['input.value']));
            assert.doesNotThrow(() => JSON.parse(attributes(model.attributes)['output.value']));
          }
        } finally {
          await provider.shutdown(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve));
        }
      });
    }
  }
}

test('README runs unchanged from a clean packed install with loopback model and OTLP', async () => {
  assert.ok(process.env.STRANDS_TEST_ROOT && process.env.STRANDS_TEST_PACKED,
    'run wire tests against the packed tarball in an isolated install');
  const { server, requests, url } = await receiver();
  const dir = await mkdtemp(join(process.env.STRANDS_TEST_ROOT, 'readme-'));
  try {
    const readme = await readFile(new URL('../README.md', import.meta.url), 'utf8');
    const snippets = [...readme.matchAll(/```javascript\n([\s\S]*?)```/g)].map((m) => m[1]);
    assert.equal(snippets.length, 2);
    await writeFile(join(dir, 'tracing.mjs'), snippets[0]);
    await writeFile(join(dir, 'app.mjs'), snippets[1]);
    await promisify(execFile)(process.execPath, [require.resolve('typescript/bin/tsc'),
      '--noEmit', '--allowJs', '--checkJs', '--module', 'nodenext', '--target', 'es2022', '--skipLibCheck',
      join(dir, 'tracing.mjs'), join(dir, 'app.mjs')]);
    const child = spawn(process.execPath, [join(dir, 'app.mjs')], { env: {
      PATH: process.env.PATH,
      FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret',
      OPENAI_API_KEY: 'synthetic-model-key', OPENAI_BASE_URL: `${url}/v1`,
      OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: `${url}/tracer/v1/traces`,
    }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.stdout.resume();
    const timer = setTimeout(() => child.kill(), 20000);
    const [code] = await once(child, 'exit'); clearTimeout(timer);
    assert.equal(code, 0, stderr);
    assertHeaders(requests);
    const list = flatten(requests);
    assert.ok(list.some((s) => attributes(s.attributes)['fi.span.kind'] === 'AGENT'));
    assert.ok(list.some((s) => attributes(s.attributes)['fi.span.kind'] === 'LLM'));
    assert.equal(list.reduce((sum, s) => sum + (attributes(s.attributes)['gen_ai.usage.total_tokens'] ?? 0), 0), 5);
    assert.ok(!JSON.stringify(requests).includes('An API connects applications.'));
  } finally {
    server.closeAllConnections(); await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  }
});
