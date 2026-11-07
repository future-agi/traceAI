// Repository smoke test. Run from typescript with ts-node/register/transpile-only
// and TS_NODE_PROJECT=packages/traceai_auto/tsconfig.json. Genkit itself is real;
// the collector is an in-process recording exporter, so no network is required.
const assert = require('node:assert/strict');
process.env.GENKIT_ENV = 'prod';
delete process.env.GENKIT_TELEMETRY_SERVER;
const { init, report: agentReport, forceFlush, shutdown } = require('../../traceai_auto/src/index.ts');
const spans = [];
const exporter = {
  export(batch, done) { spans.push(...batch); done({ code: 0 }); },
  async forceFlush() {},
  async shutdown() {},
};

(async () => {
  try {
    init({
      apiKey: 'test-api-key', secretKey: 'test-secret-key', projectName: 'genkit-bootstrap-smoke',
      instrumentations: ['genkit'], wireCapture: false, exporter,
      traceConfig: { hideInputs: true, hideOutputs: true },
    });
    const report = agentReport();
    assert.equal(report.active, true);
    assert.equal(report.integrations.find(entry => entry.name === 'genkit').status, 'instrumented');
    // Application code uses only Genkit. No manual mapper/telemetry setup/enable.
    const { genkit } = require('genkit');
    const { mockModel } = require('genkit/testing');
    const ai = genkit({});
    const model = mockModel(ai, { name: 'bootstrap/mock', respond: {
      text: 'bootstrap answer', usage: { inputTokens: 2, outputTokens: 3, totalTokens: 5 },
    } });
    assert.equal((await ai.generate({ model, prompt: 'bootstrap prompt' })).text, 'bootstrap answer');
    await forceFlush();
    const models = spans.filter(span => span.attributes['gen_ai.span.kind'] === 'LLM');
    assert.equal(models.length, 1);
    assert.equal(spans.length, 2);
    const [mapped] = models;
    assert.equal(mapped.attributes['gen_ai.request.model'], 'mock');
    assert.equal(mapped.attributes['gen_ai.usage.total_tokens'], 5);
    assert.notEqual(mapped.spanContext().traceId, '00000000000000000000000000000000');
    assert.notEqual(mapped.spanContext().spanId, '0000000000000000');
    assert.ok(mapped.duration[0] > 0 || mapped.duration[1] > 0);
    assert.equal(mapped.resource.attributes.project_name, 'genkit-bootstrap-smoke');
    assert.equal(mapped.attributes['genkit:input'], undefined);
    const exportedAttributes = JSON.stringify(spans.map(span => span.attributes));
    assert.ok(!exportedAttributes.includes('bootstrap prompt'));
    assert.ok(!exportedAttributes.includes('bootstrap answer'));
    console.log('Real @traceai/auto -> Genkit bootstrap: 14 assertions passed');
  } finally {
    await shutdown();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
