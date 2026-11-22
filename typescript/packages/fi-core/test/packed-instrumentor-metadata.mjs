// Isolated packed consumer. Both instrumentors are real; only provider I/O is mocked.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { context } from '@opentelemetry/api';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import * as esm from '@traceai/fi-core';
const require = createRequire(import.meta.url);
const cjs = require('@traceai/fi-core');
const { instrumentGoogleGenAI } = require('@traceai/google-genai-sdk');
const { BedrockInstrumentation } = require('@traceai/bedrock');
const { InvokeModelCommand } = require('@aws-sdk/client-bedrock-runtime');
for (const pkg of ['@traceai/google-genai-sdk', '@traceai/bedrock']) {
  assert.equal(createRequire(require.resolve(pkg)).resolve('@traceai/fi-core'), require.resolve('@traceai/fi-core'));
}
const memory = new InMemorySpanExporter();
const provider = cjs.register({projectName: 'instrumentor-metadata', spanProcessors: [new SimpleSpanProcessor(memory)]});
const google = {models: {async generateContent() { return {candidates: []}; }}};
const handle = instrumentGoogleGenAI(google, {tracerProvider: provider});
class BedrockRuntimeClient { async send() { return {body: new TextEncoder().encode('{}')}; } }
const bedrock = new BedrockInstrumentation({instrumentationConfig: {enabled: false}, tracerProvider: provider});
bedrock.manuallyInstrument({BedrockRuntimeClient});
try {
  const key = 'fi.context.session_id';
  for (const [label, synthetic, call] of [
    ['google', '12345678-1111-4111-8111-111111111111', () => google.models.generateContent({model: 'fixture', contents: 'hello'})],
    ['bedrock', '12345678-2222-4222-8222-222222222222', () => new BedrockRuntimeClient().send(new InvokeModelCommand({modelId: 'fixture', body: '{}'}))],
  ]) {
    await context.with(cjs.setAttributes(context.active(), {[key]: synthetic, note: `fixture ${synthetic}`, ordinary: label}), call);
    const spans = memory.getFinishedSpans();
    assert.equal(spans.length, 1, label);
    const [raw] = spans;
    assert.equal(raw.attributes.ordinary, label);
    for (const [first, second] of [[esm, cjs], [cjs, esm]]) {
      const view = first.privacyFilter().span(raw);
      for (const exported of [view, second.privacyFilter().span(view)]) {
        assert.equal(exported.attributes[key], '<redacted>');
        assert.equal(exported.attributes.ordinary, label);
        assert.ok(!JSON.stringify(exported).includes(synthetic), label);
      }
    }
    memory.reset();
  }
  console.log('PASS: packed GoogleGenAI start options and Bedrock setter replay; two detached export boundaries');
} finally { handle.dispose(); await provider.shutdown(); }
