/** Public context helpers and background operations, real Genkit -> loopback HTTP -> OTLP. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { writeFileSync } from 'node:fs';
import { context } from '@opentelemetry/api';
import { genkit } from 'genkit/beta';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { register, ProjectType, setAttributes, setPromptTemplate } from '@traceai/fi-core';
import { FIGenkitSpanProcessor } from '@traceai/genkit';

const flags = {
  hideInputs: 'FI_HIDE_INPUTS', hideInputMessages: 'FI_HIDE_INPUT_MESSAGES', hideInputText: 'FI_HIDE_INPUT_TEXT',
  hideInputImages: 'FI_HIDE_INPUT_IMAGES', hideOutputs: 'FI_HIDE_OUTPUTS', hideOutputMessages: 'FI_HIDE_OUTPUT_MESSAGES',
  hideOutputText: 'FI_HIDE_OUTPUT_TEXT', hideErrorDetails: 'FI_HIDE_ERROR_DETAILS', hideEmbeddingVectors: 'FI_HIDE_EMBEDDING_VECTORS',
};
const cases = ['context', 'operation'].flatMap(mode => Object.keys(flags)
  .filter(flag => mode === 'operation' || !['hideErrorDetails', 'hideEmbeddingVectors'].includes(flag))
  .flatMap(flag => [false, true].map(hidden => ({ name: `${mode}-${flag}-${hidden}`, mode, flag, hidden, env: { [flags[flag]]: String(hidden) } }))));
for (const mode of ['context', 'operation']) {
  cases.push({ name: `${mode}-option`, mode, flag: 'hideInputs', hidden: true, config: { hideInputs: true } });
  cases.push({ name: `${mode}-override`, mode, flag: 'hideInputs', hidden: false, env: { FI_HIDE_INPUTS: 'true' }, config: { hideInputs: false } });
  cases.push({ name: `${mode}-capture-off`, mode, flag: 'hideInputs', hidden: true, captureContent: false });
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
  Object.assign(process.env, test.env);
  const marker = 'SYNTHETIC_CONTEXT_OPERATION_WIRE_MARKER';
  const payloads = [], requests = [], native = [];
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/model') {
      requests.push({ path: req.url, body });
      res.end(JSON.stringify({ message: { role: 'model', content: [{ text: 'public reply' }] }, finishReason: 'stop', usage: { inputTokens: 4, outputTokens: 2, totalTokens: 6 } }));
    } else if (req.url === '/operations') {
      requests.push({ path: req.url, body });
      res.end(JSON.stringify({ id: 'public-op', done: true, metadata: { originalRequest: body, opaqueText: marker },
        output: { message: { role: 'model', content: [{ text: 'public reply' }] }, finishReason: 'stop', usage: { inputTokens: 4, outputTokens: 2, totalTokens: 6 } } }));
    } else {
      payloads.push(body); res.end('{}');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const post = async (path, body) => (await fetch(origin + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
  const provider = register({ projectName: 'genkit-content-contract', projectType: ProjectType.OBSERVE,
    setGlobalTracerProvider: false, endpoint: origin + '/v1/traces', headers: { 'X-Api-Key': 'synthetic', 'X-Secret-Key': 'synthetic' } });
  const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig: test.config, captureContent: test.captureContent });
  const snapshot = span => JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status });
  try {
    await enableTelemetry({ spanProcessors: [{ onStart() {}, onEnd(span) { native.push({ span, original: snapshot(span) }); }, async forceFlush() {}, async shutdown() {} }, processor], autoDetectResources: false });
    const ai = genkit({ plugins: [] });
    if (test.mode === 'context') {
      const model = ai.defineModel({ name: 'offline/context', supports: { multiturn: true } }, request => post('/model', request));
      const output = test.flag.startsWith('hideOutput');
      const side = output ? 'output' : 'input';
      const content = test.flag === 'hideInputImages' ? [{ type: 'image_url', image_url: { url: `https://example.invalid/${marker}` } }] : marker;
      let ctx = setAttributes(context.active(), { [`${side}.value`]: marker, [`raw.${side}`]: marker,
        [`gen_ai.${side}.messages`]: JSON.stringify([{ role: output ? 'assistant' : 'user', content }]) });
      if (!output) ctx = setPromptTemplate(ctx, { template: 'public {{value}}', variables: { value: marker } });
      const flow = ai.defineFlow({ name: 'public-flow' }, async () => (await ai.generate({ model, prompt: 'public prompt' })).text);
      assert.equal(await context.with(ctx, () => flow()), 'public reply');
    } else {
      const model = ai.defineBackgroundModel({ name: 'offline/background', supports: { longRunning: true },
        start: request => post('/operations', { ...request, synthetic: marker }), async check(op) { return op; }, async cancel(op) { return op; } });
      const operation = await ai.generateOperation({ model, prompt: 'public prompt' });
      const original = JSON.stringify(operation);
      assert.ok(original.includes(marker));
      await ai.checkOperation(operation);
      await ai.cancelOperation(operation);
      assert.equal(JSON.stringify(operation), original, 'application metadata is unchanged');
    }
    await flushTracing(); await processor.forceFlush();
    // Store complete request and OTLP payload evidence before evaluating privacy assertions.
    if (process.env.PRIVACY_EVIDENCE_DIR) writeFileSync(`${process.env.PRIVACY_EVIDENCE_DIR}/${test.name}.json`, JSON.stringify({ requests, payloads }, null, 2));
    assert.equal(requests.length, 1, 'one loopback provider request');
    const spans = payloads.flatMap(p => p.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
    assert.ok(spans.length >= (test.mode === 'context' ? 3 : 4));
    assert.equal(spans.length, native.length);
    assert.equal(new Set(spans.map(s => s.spanId)).size, spans.length);
    // Includes attributes, events, links, status, instrumentation scopes and resource fields.
    assert.equal(JSON.stringify(payloads).includes(marker), !test.hidden, 'complete OTLP payload follows content setting');
    const attrs = s => Object.fromEntries((s.attributes ?? []).map(a => [a.key, a.value.stringValue ?? a.value.intValue]));
    if (test.hidden) {
      assert.ok(JSON.stringify(payloads).includes('__REDACTED__'), 'documented placeholder present');
      if (test.mode === 'context') {
        const side = test.flag.startsWith('hideOutput') ? 'output' : 'input';
        for (const span of spans) assert.equal(attrs(span)[`${side}.value`], '__REDACTED__');
      } else {
        const copies = spans.flatMap(s => s.attributes ?? []).filter(a => ['genkit:input', 'genkit:output', 'input.value', 'output.value'].includes(a.key));
        const operations = copies.flatMap(a => { try { const v = JSON.parse(a.value.stringValue); return [v?.operation ?? v]; } catch { return []; } }).filter(v => v?.id === 'public-op');
        if (test.captureContent === false) {
          assert.equal(operations.length, 0, 'both complete envelopes are hidden');
          assert.ok(copies.length > 0);
          for (const copy of copies) assert.equal(copy.value.stringValue, '__REDACTED__');
        } else {
          assert.ok(operations.length > 0, 'operation structure survives');
          for (const op of operations) assert.equal(op.metadata, '__REDACTED__');
        }
      }
    } else {
      assert.ok(spans.some(s => JSON.stringify(s.attributes).includes(marker)), 'positive exported marker');
    }
    for (const { span, original } of native) {
      assert.equal(snapshot(span), original, 'native spans unchanged');
      const exported = spans.find(s => s.spanId === span.spanContext().spanId);
      assert.equal(exported.traceId, span.spanContext().traceId);
      assert.equal(exported.parentSpanId || undefined, span.parentSpanContext?.spanId ?? span.parentSpanId);
    }
    console.log(JSON.stringify({ case: test.name, spans: spans.length, requests: requests.length, hidden: test.hidden, result: 'PASS' }));
  } finally { await processor.shutdown(); await new Promise(resolve => server.close(resolve)); }
}
