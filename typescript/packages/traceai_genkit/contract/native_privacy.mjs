/** Native registered generation, media, and background operations -> loopback OTLP. */
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { genkit } from 'genkit/beta';
import { mockModel } from 'genkit/testing';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { register, ProjectType } from '@traceai/fi-core';
import { FIGenkitSpanProcessor } from '@traceai/genkit';

const cases = [
  ...['hideInputs', 'hideInputMessages', 'hideInputText'].map((flag) => ({ name: `action-${flag}`, mode: 'action', config: { [flag]: true }, hidden: true })),
  { name: 'action-default', mode: 'action', hidden: false },
  ...['generate', 'action', 'background'].flatMap((entry) => [
    { name: `image-${entry}`, mode: 'image', entry, config: { hideInputImages: true }, hidden: true },
    { name: `image-env-${entry}`, mode: 'image', entry, env: { FI_HIDE_INPUT_IMAGES: 'true' }, hidden: true },
    { name: `image-override-${entry}`, mode: 'image', entry, config: { hideInputImages: false }, env: { FI_HIDE_INPUT_IMAGES: 'true' }, hidden: false },
  ]),
  ...['start', 'check', 'cancel'].flatMap((entry) => [
    { name: `operation-${entry}`, mode: 'operation', entry, config: { hideErrorDetails: true }, hidden: true },
    { name: `operation-implied-${entry}`, mode: 'operation', entry, config: { hideInputText: true }, hidden: true },
    { name: `operation-override-${entry}`, mode: 'operation', entry, config: { hideInputText: true, hideErrorDetails: false }, hidden: false },
  ]),
];

if (!process.argv[2]) {
  let failed = 0;
  for (const test of cases) {
    const child = spawnSync(process.execPath, [fileURLToPath(import.meta.url), test.name], { env: process.env, encoding: 'utf8', timeout: 30000 });
    process.stdout.write(child.stdout ?? '');
    if (child.status !== 0) {
      failed++;
      process.stderr.write(`${test.name}: ${child.error ?? child.stderr}\n`);
    }
  }
  console.log(JSON.stringify({ cases: cases.length, passed: cases.length - failed, failed }));
  process.exitCode = failed ? 1 : 0;
} else {
  const test = cases.find(({ name }) => name === process.argv[2]);
  assert.ok(test, 'unknown case');
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('FI_') || key.startsWith('OTEL_') || key === 'GENKIT_TELEMETRY_SERVER') delete process.env[key];
  }
  Object.assign(process.env, test.env);
  const marker = 'SYNTHETIC_PRIVATE_NATIVE_CONTENT';
  const reply = 'SYNTHETIC_PUBLIC_REPLY';
  const payloads = [];
  const native = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    payloads.push(JSON.parse(Buffer.concat(chunks)));
    res.writeHead(200, { 'Content-Type': 'application/json' }).end('{}');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const provider = register({
    projectName: 'genkit-native-privacy', projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false,
    endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`, headers: { 'X-Api-Key': 'synthetic', 'X-Secret-Key': 'synthetic' },
  });
  const processor = new FIGenkitSpanProcessor({ tracerProvider: provider, traceConfig: test.config });
  const snapshot = (span) => JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status });
  try {
    await enableTelemetry({ spanProcessors: [{
      onStart() {}, onEnd(span) { native.push({ span, original: snapshot(span) }); }, async forceFlush() {}, async shutdown() {},
    }, processor], autoDetectResources: false });
    const ai = genkit({ plugins: [] });
    const model = mockModel(ai, { name: 'offline/public', info: { supports: { media: true } }, respond: () => ({ text: reply }) });
    const registered = await ai.registry.lookupAction('/util/generate');
    assert.ok(registered);
    if (test.mode === 'action') {
      const response = await registered({ model: 'offline/public', messages: [{ role: 'user', content: [{ text: marker }] }] });
      assert.equal(response.message.content[0].text, reply);
      assert.ok(JSON.stringify(model.lastRequest).includes(marker));
    } else if (test.mode === 'image') {
      const content = [{ text: 'public input text' }, { media: { url: `https://example.invalid/${marker}.png`, contentType: 'image/png' } }];
      if (test.entry === 'action') {
        const response = await registered({ model: 'offline/public', messages: [{ role: 'user', content }] });
        assert.equal(response.message.content[0].text, reply);
      } else if (test.entry === 'background') {
        const bg = ai.defineBackgroundModel({
          name: 'offline/image-background', supports: { longRunning: true, media: true },
          async start(request) {
            assert.ok(JSON.stringify(request).includes(marker));
            return { id: 'image-op', done: true, output: { message: { role: 'model', content: [{ text: reply }] }, finishReason: 'stop', request } };
          },
          async check(op) { return op; }, async cancel(op) { return op; },
        });
        const op = await ai.generateOperation({ model: bg, prompt: content });
        assert.ok(JSON.stringify(op).includes(marker), 'application operation retains input');
        await ai.checkOperation(op);
        await ai.cancelOperation(op);
      } else {
        assert.equal((await ai.generate({ model, prompt: content })).text, reply);
      }
      if (test.entry !== 'background') assert.ok(JSON.stringify(model.lastRequest).includes(marker));
    } else {
      const error = { message: marker, details: { private: marker }, status: 'INTERNAL', code: 13 };
      const bg = ai.defineBackgroundModel({
        name: 'offline/background', supports: { longRunning: true },
        async start() { return { id: 'synthetic-op', done: test.entry === 'start', ...(test.entry === 'start' ? { error } : {}), metadata: { stage: 'safe' } }; },
        async check(op) { return { ...op, done: true, error }; },
        async cancel(op) { return { ...op, done: true, error }; },
      });
      const op = await ai.generateOperation({ model: bg, prompt: 'public prompt' });
      const completed = test.entry === 'start' ? op : test.entry === 'check' ? await ai.checkOperation(op) : await ai.cancelOperation(op);
      assert.equal(completed.error.message, marker);
      const original = JSON.stringify(completed);
      // Both input and output Operation copies must respect error privacy, including re-polling.
      await ai.checkOperation(completed);
      await ai.cancelOperation(completed);
      assert.equal(JSON.stringify(completed), original, 'application operation is unchanged');
    }
    await flushTracing();
    await processor.forceFlush();
    const wire = payloads.flatMap((p) => p.resourceSpans ?? []).flatMap((r) => r.scopeSpans).flatMap((s) => s.spans);
    assert.ok(wire.length > 0);
    if (process.env.PRIVACY_EVIDENCE_DIR) writeFileSync(`${process.env.PRIVACY_EVIDENCE_DIR}/${test.name}.json`, JSON.stringify({ case: test.name, payloads, native: native.map(({ span }) => ({ name: span.name, attributes: span.attributes })) }));
    assert.equal(wire.length, native.length);
    assert.equal(new Set(wire.map((s) => s.spanId)).size, wire.length);
    const leaks = wire.flatMap((s) => (s.attributes ?? []).filter((a) => JSON.stringify(a.value).includes(marker)).map((a) => `${s.name}:${a.key}`));
    console.log(JSON.stringify({ case: test.name, spans: wire.length, leaks }));
    assert.equal(JSON.stringify(wire).includes(marker), !test.hidden, 'native content policy on the OTLP wire');
    if (test.mode !== 'operation') assert.ok(JSON.stringify(wire).includes(reply), 'public output survives');
    if (test.mode === 'image') assert.ok(JSON.stringify(wire).includes('public input text'), 'image-only policy preserves input text');
    if (test.mode === 'operation') {
      const outputs = wire.flatMap((s) => s.attributes ?? []).filter((a) => a.key === 'genkit:output').map((a) => JSON.parse(a.value.stringValue));
      const operations = outputs.map((o) => o.operation ?? o).filter((o) => o.id === 'synthetic-op');
      assert.ok(operations.some((o) => o.done && o.error));
      for (const op of operations) {
        assert.equal(op.action, '/background-model/offline/background');
        assert.equal(op.metadata, '__REDACTED__', 'opaque state follows content/error hiding even when error text is opted in');
        if (op.error) {
          assert.equal(op.error.status, 'INTERNAL');
          assert.equal(op.error.code, 13);
        }
      }
    }
    for (const { span, original } of native) {
      assert.equal(snapshot(span), original, 'native span unchanged');
      const exported = wire.find((s) => s.spanId === span.spanContext().spanId);
      assert.ok(exported);
      assert.equal(exported.traceId, span.spanContext().traceId);
      assert.equal(exported.parentSpanId || undefined, span.parentSpanContext?.spanId ?? span.parentSpanId);
    }
  } finally {
    await processor.shutdown();
    await new Promise((resolve) => server.close(resolve));
  }
}
