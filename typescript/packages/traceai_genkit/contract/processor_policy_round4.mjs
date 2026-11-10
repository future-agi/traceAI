/** Processor-only tool and resource policy -> loopback provider -> complete OTLP. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { writeFileSync } from 'node:fs';
import { context, trace } from '@opentelemetry/api';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { genkit } from 'genkit';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { register, ProjectType, setAttributes } from '@traceai/fi-core';
import { FIGenkitSpanProcessor } from '@traceai/genkit';

const cases = [];
for (const surface of ['context', 'event', 'link', 'resource']) {
  const keys = ['gen_ai.tool.call.arguments', 'gen_ai.tool.description', 'gen_ai.request.stop_sequences', 'gen_ai.tool.call.result'];
  if (surface === 'resource') keys.push('gen_ai.prompt', 'gen_ai.system_instructions', 'gen_ai.completion');
  for (const key of keys) {
    for (const hidden of [false, true]) {
      cases.push({ name: `${surface}-${key}-${hidden}`, surface, key, hidden,
        flag: ['gen_ai.completion', 'gen_ai.tool.call.result'].includes(key) ? 'hideOutputs' : 'hideInputs' });
    }
  }
}
if (!process.argv[2]) {
  let failed = 0;
  for (const test of cases) {
    const child = spawnSync(process.execPath, [fileURLToPath(import.meta.url), test.name], { env: process.env, encoding: 'utf8', timeout: 30000 });
    process.stdout.write(child.stdout ?? '');
    if (child.status !== 0) { failed++; process.stderr.write(`${test.name}: ${child.error ?? child.stderr}\n`); }
  }
  console.log(JSON.stringify({ cases: cases.length, passed: cases.length - failed, failed }));
  process.exitCode = failed ? 1 : 0;
} else {
  const test = cases.find(t => t.name === process.argv[2]);
  assert.ok(test);
  for (const key of Object.keys(process.env)) if (key.startsWith('FI_') || key.startsWith('OTEL_') || key === 'GENKIT_TELEMETRY_SERVER') delete process.env[key];
  const marker = 'SYNTHETIC_SURFACE_PRIVACY_MARKER';
  const payloads = [], requests = [], native = [];
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/model') {
      requests.push({ path: req.url, body });
      res.end(JSON.stringify({ message: { role: 'model', content: [{ text: 'public reply' }] }, finishReason: 'stop' }));
    } else { payloads.push(body); res.end('{}'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const attributes = { [test.key]: marker };
  const resource = resourceFromAttributes({ 'service.name': 'public-service', 'deployment.environment.name': 'test',
    ...(test.surface === 'resource' ? attributes : {}) });
  const resourceBefore = JSON.stringify(resource.attributes);
  // Exporter defaults deliberately remain independent of the processor option.
  const provider = register({ projectName: 'genkit-surface-contract', projectType: ProjectType.OBSERVE,
    resource, setGlobalTracerProvider: false, endpoint: origin + '/v1/traces', headers: { 'X-Api-Key': 'synthetic', 'X-Secret-Key': 'synthetic' } });
  const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig: { [test.flag]: test.hidden } });
  const snapshot = span => JSON.stringify({ attributes: span.attributes, events: span.events, links: span.links, status: span.status, resource: span.resource.attributes });
  const capture = { onStart() {}, onEnd(span) { native.push({ span, original: snapshot(span) }); }, async forceFlush() {}, async shutdown() {} };
  try {
    await enableTelemetry({ spanProcessors: [capture, processor], autoDetectResources: false });
    const ai = genkit({ plugins: [] });
    const model = ai.defineModel({ name: 'offline/surface' }, async request => (await fetch(origin + '/model', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
    })).json());
    const flow = ai.defineFlow('public-surface-flow', async () => {
      if (test.surface === 'event') trace.getActiveSpan().addEvent('synthetic-content', attributes);
      if (test.surface === 'link') trace.getActiveSpan().addLink({ context: {
        traceId: '11111111111111111111111111111111', spanId: '2222222222222222', traceFlags: 1,
      }, attributes });
      return (await ai.generate({ model, prompt: 'public prompt' })).text;
    });
    const ctx = test.surface === 'context' ? setAttributes(context.active(), attributes) : context.active();
    assert.equal(await context.with(ctx, () => flow()), 'public reply');
    await flushTracing(); await processor.forceFlush();
    if (process.env.PRIVACY_EVIDENCE_DIR) writeFileSync(`${process.env.PRIVACY_EVIDENCE_DIR}/${test.name}.json`, JSON.stringify({ requests, payloads }, null, 2));
    assert.equal(requests.length, 1, 'exactly one local provider request');
    const spans = payloads.flatMap(p => p.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
    assert.equal(spans.length, 3);
    assert.equal(new Set(spans.map(s => s.spanId)).size, spans.length);
    assert.equal(JSON.stringify(payloads).includes(marker), !test.hidden, 'complete OTLP payload follows processor content policy');
    const selected = test.surface === 'resource'
      ? payloads.flatMap(p => p.resourceSpans ?? []).flatMap(r => r.resource.attributes ?? [])
      : test.surface === 'context' ? spans.flatMap(s => s.attributes ?? [])
      : spans.flatMap(s => s[test.surface === 'event' ? 'events' : 'links'] ?? []).flatMap(s => s.attributes ?? []);
    const content = selected.filter(a => Object.hasOwn(attributes, a.key));
    assert.ok(content.length > 0, 'selected content attribute is exported, not a vacuous control');
    for (const attr of content) assert.equal(attr.value.stringValue, test.hidden ? '__REDACTED__' : marker,
      'selected surface uses documented placeholder or original content');
    for (const r of payloads.flatMap(p => p.resourceSpans ?? [])) {
      const attrs = Object.fromEntries(r.resource.attributes.map(a => [a.key, a.value.stringValue]));
      assert.equal(attrs['service.name'], 'public-service', 'resource identity survives');
      assert.equal(attrs['deployment.environment.name'], 'test');
      assert.equal(attrs.project_name, 'genkit-surface-contract', 'FI routing survives');
      assert.equal(attrs.project_type, 'observe');
    }
    assert.equal(JSON.stringify(resource.attributes), resourceBefore, 'application resource unchanged');
    for (const { span, original } of native) {
      assert.equal(snapshot(span), original, 'native span unchanged');
      const exported = spans.find(s => s.spanId === span.spanContext().spanId);
      assert.equal(exported.traceId, span.spanContext().traceId);
      assert.equal(exported.parentSpanId || undefined, span.parentSpanContext?.spanId ?? span.parentSpanId);
    }
    console.log(JSON.stringify({ case: test.name, passed: true, spans: spans.length, requests: requests.length }));
  } finally { await processor.shutdown(); await new Promise(resolve => server.close(resolve)); }
}
