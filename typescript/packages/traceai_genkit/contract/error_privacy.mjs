/** Offline regression: real Genkit spans -> fi-core's HTTP exporter -> localhost OTLP JSON. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { genkit } from 'genkit';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { register, ProjectType } from '@traceai/fi-core';
import { FIGenkitSpanProcessor } from '@traceai/genkit';

const marker = 'SYNTHETIC_PRIVATE_ERROR_MARKER';
const contentFlags = [
  ['hideInputs', 'FI_HIDE_INPUTS'],
  ['hideOutputs', 'FI_HIDE_OUTPUTS'],
  ['hideInputMessages', 'FI_HIDE_INPUT_MESSAGES'],
  ['hideOutputMessages', 'FI_HIDE_OUTPUT_MESSAGES'],
  ['hideInputText', 'FI_HIDE_INPUT_TEXT'],
  ['hideOutputText', 'FI_HIDE_OUTPUT_TEXT'],
];
const cases = [
  { name: 'default', hidden: false },
  { name: 'option', config: { hideErrorDetails: true }, hidden: true },
  { name: 'env', env: { FI_HIDE_ERROR_DETAILS: 'true' }, hidden: true },
  { name: 'env-whitespace', env: { FI_HIDE_ERROR_DETAILS: ' TRUE ' }, hidden: true },
  ...contentFlags.flatMap(([key, env]) => [
    { name: key, config: { [key]: true }, hidden: true },
    { name: env, env: { [env]: 'true' }, hidden: true },
  ]),
  { name: 'option-false-wins', config: { hideInputs: true, hideErrorDetails: false }, env: { FI_HIDE_ERROR_DETAILS: 'true' }, hidden: false },
  { name: 'env-false-wins', config: { hideInputText: true }, env: { FI_HIDE_ERROR_DETAILS: 'false' }, hidden: false },
  { name: 'option-true-wins', config: { hideErrorDetails: true }, env: { FI_HIDE_ERROR_DETAILS: 'false' }, hidden: true },
  { name: 'invalid-env-derives', config: { hideOutputMessages: true }, env: { FI_HIDE_ERROR_DETAILS: 'not-a-boolean' }, hidden: true },
  { name: 'empty-env-derives', config: { hideInputText: true }, env: { FI_HIDE_ERROR_DETAILS: '' }, hidden: true },
  { name: 'vectors-only', config: { hideEmbeddingVectors: true }, hidden: false },
  { name: 'images-only', config: { hideInputImages: true }, hidden: false },
  { name: 'legacy-captureContent', captureContent: false, hidden: true },
];

if (!process.argv[2]) {
  let failed = 0;
  for (const test of cases) {
    const child = spawnSync(process.execPath, [fileURLToPath(import.meta.url), test.name], {
      env: process.env, encoding: 'utf8', timeout: 30000,
    });
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
  assert.ok(test, 'unknown test case');
  // No ambient privacy settings or credentials; the only exporter endpoint is localhost.
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('FI_') || key.startsWith('OTEL_') || key === 'GENKIT_TELEMETRY_SERVER') delete process.env[key];
  }
  Object.assign(process.env, test.env);
  const payloads = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    payloads.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end('{}');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const provider = register({
    projectName: 'genkit-offline-privacy', projectType: ProjectType.OBSERVE,
    setGlobalTracerProvider: false,
    endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`,
    headers: { 'x-api-key': 'synthetic', 'x-secret-key': 'synthetic' },
  });
  const processor = new FIGenkitSpanProcessor({
    tracerProvider: provider, traceConfig: test.config, captureContent: test.captureContent,
  });
  const native = [];
  const capture = {
    onStart() {},
    onEnd(span) {
      native.push({ span, original: JSON.stringify({ status: span.status, attributes: span.attributes, events: span.events }) });
    },
    async forceFlush() {}, async shutdown() {},
  };
  try {
    await enableTelemetry({ spanProcessors: [capture, processor], autoDetectResources: false });
    const ai = genkit({ plugins: [] });
    const tool = ai.defineTool({ name: 'offlineFailure', description: 'synthetic failure' }, async () => {
      throw new Error(marker);
    });
    const failure = ai.defineFlow('offlineErrorFlow', async () => tool({}));
    const success = ai.defineFlow('offlineSuccessFlow', async () => 'synthetic success');
    await success();
    await assert.rejects(failure(), { message: marker });
    await flushTracing();
    await processor.forceFlush();

    const wire = payloads.flatMap((p) => p.resourceSpans ?? []).flatMap((r) => r.scopeSpans).flatMap((s) => s.spans);
    assert.equal(wire.length, 3, 'one export per native flow/tool span');
    assert.equal(new Set(wire.map((s) => s.spanId)).size, wire.length, 'no duplicate spans');
    const errorSpans = wire.filter((s) => s.status?.code === 2);
    assert.equal(errorSpans.length, 2, 'flow and tool both retain ERROR status');
    assert.equal(JSON.stringify(wire).includes(marker), !test.hidden, 'private marker on OTLP wire');
    for (const { span, original } of native) {
      assert.equal(JSON.stringify({ status: span.status, attributes: span.attributes, events: span.events }), original, 'native span is unchanged');
      const exported = wire.find((s) => s.spanId === span.spanContext().spanId);
      assert.ok(exported, 'span id preserved');
      assert.equal(exported.traceId, span.spanContext().traceId);
      assert.equal(exported.parentSpanId || undefined, span.parentSpanContext?.spanId ?? span.parentSpanId);
    }
    for (const span of errorSpans) {
      const exception = span.events.find((e) => e.name === 'exception');
      assert.ok(exception.attributes.some((a) => a.key === 'exception.type' && a.value.stringValue === 'Error'));
      assert.ok(!exception.attributes.some((a) => a.key === 'exception.stacktrace'));
      if (test.hidden) {
        assert.ok(!span.status.message, 'hidden status has no description');
        assert.ok(!exception.attributes.some((a) => a.key === 'exception.message'));
      }
    }
    console.log(JSON.stringify({ case: test.name, passed: true, spans: wire.length, errorSpans: errorSpans.length, privateErrorMarkerExported: !test.hidden }));
  } finally {
    await processor.shutdown();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}
