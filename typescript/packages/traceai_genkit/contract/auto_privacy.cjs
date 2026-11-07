// Repository regression probe: current real auto-agent + real Genkit + mock model.
// Run once per mode under heavy.sh. Only a synthetic loopback transport is used.
// S5 replaces request-local discovery with core transport observation; marked
// credentials are sent once before the old mapping/lifecycle regressions run.
const assert = require('node:assert/strict');
const http = require('node:http');
process.env.GENKIT_ENV = 'prod';
delete process.env.GENKIT_TELEMETRY_SERVER;
process.env.OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT = '600';
const mode = process.argv[2];
const modes = ['truncated', 'masked-secret', 'context-secret', 'inflight', 'request-field', 'error-override', 'tail-secret', 'context-tail-secret', 'child-secret', 'invocation'];
if (mode === 'invocation') process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS = 'true';
assert.ok(modes.includes(mode), 'Pass one of: ' + modes.join(', '));
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { init, report: agentReport, forceFlush, shutdown } = require('../../traceai_auto/src/index.ts');
const { enable } = require('../dist/src/index.js');
const nativeExporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(nativeExporter)] });
assert.equal(trace.setGlobalTracerProvider(provider), true);
const spans = [];
const exporter = {
  export(batch, done) { spans.push(...batch); done({ code: 0 }); },
  async forceFlush() {}, async shutdown() {},
};
const traceConfig = ['context-secret', 'context-tail-secret', 'request-field', 'invocation'].includes(mode) ? {} : {
  hideInputs: true, ...(mode === 'error-override' ? { hideErrorDetails: false } : {}),
};
const token = 'credential-' + mode + '-verify-12345';
const server = http.createServer((req, res) => { req.resume(); res.end('ok'); });

(async () => {
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    // Deliberately leave Genkit disabled in the catalog, so inflight activation
    // can occur after the real agent has indexed the ORIGINAL native span.
    init({
      apiKey: 'test-api-key', secretKey: 'test-secret-key', projectName: 'genkit-privacy-regression',
      instrumentations: [], wireCapture: true, exporter, traceConfig,
    });
    assert.equal(agentReport().active, true);
    const observed = await fetch('http://127.0.0.1:' + server.address().port + '/credential', { headers: { authorization: 'Bearer ' + token } });
    assert.equal(await observed.text(), 'ok');
    const { genkit, z } = require('genkit');
    const { mockModel } = require('genkit/testing');
    const ai = genkit({});
    if (mode !== 'inflight') assert.equal(enable().status, 'instrumented');
    if (mode === 'truncated') {
      const marker = 'HIDDEN_PROMPT_VERIFY_';
      const model = mockModel(ai, { name: 'privacy/mock', respond: {
        text: 'visible answer', usage: { inputTokens: 2, outputTokens: 3, totalTokens: 5 },
      } });
      assert.equal((await ai.generate({ model, prompt: marker + 'p'.repeat(1000) })).text, 'visible answer');
      await forceFlush();
      const native = nativeExporter.getFinishedSpans().find(span => span.name === 'generate');
      assert.equal(native.attributes['genkit:output'].length, 600);
      assert.ok(native.attributes['genkit:output'].includes(marker));
      assert.throws(() => JSON.parse(native.attributes['genkit:output']));
      assert.equal(spans.find(span => span.spanContext().spanId === native.spanContext().spanId).attributes['output.value'], '__REDACTED__');
      assert.ok(!JSON.stringify(spans).includes(marker));
    } else if (mode === 'inflight') {
      let entered, release;
      const started = new Promise(resolve => { entered = resolve; });
      const suspended = new Promise(resolve => { release = resolve; });
      const flow = ai.defineFlow({ name: 'inflightFlow', inputSchema: z.string(), outputSchema: z.string() }, async () => {
        entered(); await suspended; return 'response echoes ' + token;
      });
      const result = flow('ordinary input');
      await started;
      try { assert.equal(enable().status, 'instrumented'); }
      finally { release(); }
      assert.equal(await result, 'response echoes ' + token);
    } else if (mode === 'tail-secret' || mode === 'context-tail-secret') {
      const flow = ai.defineFlow({ name: 'tailFlow', inputSchema: z.any(), outputSchema: z.string() }, async () => 'response echoes ' + token);
      const result = mode === 'tail-secret'
        ? await flow({ padding: 'p'.repeat(1000), apiKey: token })
        : await flow('visible input', { context: { headers: { 'x-padding': 'p'.repeat(1000), 'x-api-key': token } } });
      assert.equal(result, 'response echoes ' + token);
      await forceFlush();
      const native = nativeExporter.getFinishedSpans()[0];
      const evidence = native.attributes[mode === 'tail-secret' ? 'genkit:input' : 'genkit:metadata:context'];
      assert.equal(evidence.length, 600);
      assert.ok(!evidence.includes(token));
      assert.throws(() => JSON.parse(evidence));
      assert.ok(spans[0].attributes['output.value'].includes('response echoes <redacted>'));
    } else if (mode === 'child-secret') {
      const tool = ai.defineTool({ name: 'credentialEchoTool', description: 'echo', inputSchema: z.string(), outputSchema: z.string() }, async () => 'response echoes ' + token);
      const flow = ai.defineFlow({ name: 'parentCredentialFlow', inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() }, async () => {
        const result = await tool('ordinary input');
        assert.equal(nativeExporter.getFinishedSpans().length, 1);
        assert.equal(spans.length, 1); // S5 exports ended children immediately.
        return result;
      });
      assert.equal(await flow({ apiKey: token }), 'response echoes ' + token);
      await forceFlush();
      assert.equal(spans.length, 2);
      for (const span of spans) assert.ok(span.attributes['output.value'].includes('response echoes <redacted>'));
    } else if (mode === 'invocation') {
      const config = { temperature: 0.314159, topP: 0.271828, maxOutputTokens: 37 };
      const model = mockModel(ai, { name: 'invocation/mock', respond: { text: 'visible answer' } });
      assert.equal((await ai.generate({ model, prompt: 'visible prompt', config })).text, 'visible answer');
      await forceFlush();
      assert.equal(spans.length, 2);
      for (const span of spans) {
        const input = JSON.parse(span.attributes['input.value']);
        assert.equal(input.config, undefined);
        assert.ok(JSON.stringify(input).includes('visible prompt'));
        assert.equal(span.attributes['gen_ai.request.parameters'], undefined);
      }
      for (const text of ['temperature', '0.314159', 'topP', '0.271828', 'maxOutputTokens']) assert.ok(!JSON.stringify(spans).includes(text));
      for (const span of nativeExporter.getFinishedSpans()) assert.deepEqual(JSON.parse(span.attributes['genkit:input']).config, config);
    } else if (mode === 'masked-secret') {
      const flow = ai.defineFlow({ name: 'maskedFlow', inputSchema: z.object({ apiKey: z.string() }), outputSchema: z.string() },
        async ({ apiKey }) => 'response echoes ' + apiKey);
      assert.equal(await flow({ apiKey: token }), 'response echoes ' + token);
    } else if (mode === 'context-secret') {
      const flow = ai.defineFlow({ name: 'contextFlow', inputSchema: z.string(), outputSchema: z.string() }, async () => 'response echoes ' + token);
      assert.equal(await flow('visible input', { context: { headers: { 'x-api-key': token } } }), 'response echoes ' + token);
    } else if (mode === 'request-field') {
      const value = { request: { id: 7 }, status: 'accepted' };
      const flow = ai.defineFlow({ name: 'requestFlow', inputSchema: z.string(), outputSchema: z.any() }, async () => value);
      assert.deepEqual(await flow('visible input'), value);
      await forceFlush();
      assert.deepEqual(JSON.parse(spans[0].attributes['output.value']), value);
    } else if (mode === 'error-override') {
      const failure = new Error('ordinary diagnostic detail');
      const flow = ai.defineFlow({ name: 'errorFlow', inputSchema: z.string(), outputSchema: z.string() }, async () => { throw failure; });
      await assert.rejects(flow('hidden input'), error => error === failure);
      await forceFlush();
      assert.equal(spans[0].status.message, failure.message);
      assert.equal(spans[0].events.find(event => event.name === 'exception').attributes['exception.message'], failure.message);
    }
    await forceFlush();
    const native = nativeExporter.getFinishedSpans();
    assert.equal(spans.length, native.length);
    assert.ok(spans.length > 0);
    if (mode.endsWith('secret') || mode === 'inflight') {
      assert.ok(!JSON.stringify(spans).includes(token));
      if (!['tail-secret', 'context-tail-secret'].includes(mode)) assert.ok(spans[0].attributes['output.value'].includes('response echoes <redacted>'));
      assert.ok(JSON.stringify(native.map(span => span.attributes)).includes(token));
    }
    for (const copy of spans) {
      const original = native.find(span => span.spanContext().spanId === copy.spanContext().spanId);
      assert.ok(original);
      for (const key of ['traceId', 'spanId', 'traceFlags']) assert.equal(copy.spanContext()[key], original.spanContext()[key]);
      assert.deepEqual(copy.startTime, original.startTime);
      assert.deepEqual(copy.endTime, original.endTime);
      assert.deepEqual(copy.duration, original.duration);
    }
    console.log(JSON.stringify({ mode, status: 'passed', exported: spans.length, native: native.length }));
  } finally {
    await shutdown();
    await provider.shutdown();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
