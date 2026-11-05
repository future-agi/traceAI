import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { genkit } from 'genkit/beta';
import { mockModel } from 'genkit/testing';
import { enableTelemetry, flushTracing } from 'genkit/tracing';
import { register, ProjectType } from '@traceai/fi-core';
import { FIGenkitSpanProcessor } from '@traceai/genkit';

// Native OTel 256-character attribute limits, real Genkit models, local OTLP only.
const truncated = process.env.OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT === '256';
const marker = 'SYNTHETIC_PRIVATE_TRUNCATION_MARKER';
const payloads = [];
const native = [];
const server = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  payloads.push(JSON.parse(Buffer.concat(chunks)));
  res.writeHead(200, { 'Content-Type': 'application/json' }).end('{}');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const provider = register({ projectName: 'genkit-review-round2', projectType: ProjectType.OBSERVE,
  setGlobalTracerProvider: false, endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`,
  headers: { 'X-Api-Key': 'synthetic', 'X-Secret-Key': 'synthetic' } });
try {
  await enableTelemetry({ spanProcessors: [{
    onStart() {}, onEnd(span) { native.push(span); }, async forceFlush() {}, async shutdown() {},
  }, new FIGenkitSpanProcessor({ tracerProvider: provider,
    traceConfig: { hideInputs: true } })], autoDetectResources: false });
  const ai = genkit({ plugins: [] });
  if (truncated) {
    const model = mockModel(ai, { name: 'offline/truncated', respond: () => ({ text: 'public reply' }) });
    await ai.generate({ model, prompt: marker + ' '.repeat(2500), stepName: 'truncated-custom' });
  } else {
    const background = ai.defineBackgroundModel({ name: 'offline/tokens', supports: { longRunning: true },
      async start() { return { id: 'synthetic-op', done: true, output: {
        message: { role: 'model', content: [{ text: 'public reply' }] }, finishReason: 'stop',
        usage: { inputTokens: 13, outputTokens: 7, totalTokens: 20 },
      } }; }, async check(op) { return op; } });
    await ai.generateOperation({ model: background, prompt: 'public prompt' });
  }
  await flushTracing();
  const spans = payloads.flatMap(p => p.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(s => s.spans ?? []);
  const attrs = span => Object.fromEntries(span.attributes.map(a => [a.key, a.value.stringValue ?? a.value.intValue ?? a.value.arrayValue]));
  if (truncated) {
    const custom = spans.filter(s => s.name === 'truncated-custom');
    assert.equal(custom.length, 1);
    const source = native.find(s => s.name === 'truncated-custom');
    assert.equal(source.attributes['genkit:input'].length, 256);
    assert.throws(() => JSON.parse(source.attributes['genkit:input']));
    assert.ok(source.attributes['genkit:output'].includes(marker), 'native response did not echo prompt');
    assert.ok(!JSON.stringify(attrs(custom[0])).includes(marker), 'truncated private prompt leaked in output');
    assert.equal(attrs(custom[0])['genkit:output'], '__REDACTED__');
    assert.equal(attrs(custom[0])['output.value'], '__REDACTED__');
  } else {
    const llm = spans.filter(s => s.name === 'offline/tokens');
    assert.equal(llm.length, 1);
    const usage = attrs(llm[0]);
    assert.equal(Number(usage['gen_ai.usage.input_tokens']), 13);
    assert.equal(Number(usage['gen_ai.usage.output_tokens']), 7);
    assert.equal(Number(usage['gen_ai.usage.total_tokens']), 20);
    assert.ok(JSON.stringify(usage['gen_ai.response.finish_reasons']).includes('stop'));
    assert.ok(!spans.some(s => s.name === 'generate' && Number(attrs(s)['gen_ai.usage.total_tokens']) === 20), 'usage double counted on parent');
  }
  console.log(truncated ? 'PASS: truncated custom generation is private' : 'PASS: background LLM usage and finish reason retained');
} finally {
  await provider.shutdown();
  await new Promise(resolve => server.close(resolve));
}
