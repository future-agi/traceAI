/* Real Mastra/privacy regressions, executed sequentially under heavy.sh. */
const assert = require('node:assert/strict');
const { context, trace } = require('@opentelemetry/api');
const [scenario, format = 'cjs'] = process.argv.slice(2);
const load = name => format === 'esm' ? import(name) : Promise.resolve(require(name));
const secret = 'dummy-provider-credential-978362';
const spans = [];
const requests = [];
const credentialScenario = scenario.startsWith('secret');
const queryCredential = scenario.startsWith('secret-query');
const outerCredential = scenario.startsWith('secret-outer') || scenario.startsWith('secret-query-outer');
const loopback = queryCredential || outerCredential;
let server;
const baseline = scenario.endsWith('-baseline');
const visible = scenario.endsWith('-visible');
const embeddingScenario = scenario.startsWith('embedding');
const hideEmbeddingOutput = scenario === 'embedding-output-hidden';
const embeddingOutputs = [[0.12345, 0.6789], [[0.12345, 0.6789], [0.9876, 0.54321]],
  { result: [[0.12345, 0.6789]], model: 'mock-embedding' }, '[[0.12345,0.6789]]'];

async function main() {
  const errorBody = JSON.stringify({ error: { message: `Invalid API key: ${secret}`, type: 'invalid_request_error', code: 'invalid_api_key' } });
  let baseURL;
  if (loopback) {
    server = require('node:http').createServer((request, response) => {
      let body = '';
      request.setEncoding('utf8');
      request.on('data', chunk => { body += chunk; });
      request.on('end', () => {
        requests.push({ url: request.url, body });
        if (queryCredential) {
          assert.equal(new URL(request.url, 'http://127.0.0.1').searchParams.get('api_key'), secret);
          assert.equal(request.headers.authorization, undefined, 'query-only credentials cannot be discovered from headers');
        } else assert.equal(request.headers.authorization, `Bearer ${secret}`);
        response.writeHead(401, { 'content-type': 'application/json' });
        response.end(errorBody);
      });
    });
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    baseURL = `http://127.0.0.1:${server.address().port}/v1`;
  } else {
    globalThis.fetch = async (url, options) => {
      requests.push({ url: String(url), body: options.body });
      return new Response(errorBody, { status: 401, headers: { 'content-type': 'application/json' } });
    };
  }
  const auto = require('@traceai/auto');
  auto.init({
    apiKey: 'test-fi-key', secretKey: 'test-fi-secret', projectName: 'mastra-privacy-regression',
    instrumentations: baseline ? ['wire'] : ['mastra', 'wire'], wireCapture: true,
    traceConfig: embeddingScenario
      ? { hideEmbeddingVectors: !visible && !hideEmbeddingOutput, hideOutputs: hideEmbeddingOutput }
      : { hideLLMInvocationParameters: !credentialScenario && !visible },
    exporter: { export(batch, callback) { spans.push(...batch); callback({ code: 0 }); }, async shutdown() {}, async forceFlush() {} },
  });
  assert.equal(auto.report().integrations.find(item => item.name === 'wire').status, 'instrumented');
  const { Mastra } = await load('@mastra/core');
  const { Agent } = await load('@mastra/core/agent');
  const { Observability, BaseExporter } = await load('@mastra/observability');
  const { SpanType } = await load('@mastra/core/observability');
  const appEvents = [];
  class AppExporter extends BaseExporter {
    name = 'app';
    async _exportTracingEvent(event) { appEvents.push(event); }
    async flush() {}
    async shutdown() {}
  }
  const observability = new Observability({ configs: { app: { serviceName: 'app', exporters: [new AppExporter()] } }, sensitiveDataFilter: false });
  let model;
  if (queryCredential) {
    const { createOpenAICompatible } = await load('@ai-sdk/openai-compatible');
    model = createOpenAICompatible({ name: 'query-gateway', baseURL, queryParams: { api_key: secret } }).chatModel('gpt-4o-mini');
  } else if (credentialScenario) {
    const { createOpenAI } = await load('@ai-sdk/openai');
    const fetchWithNativeSpan = (...args) => trace.getTracer('mock-native-sdk').startActiveSpan('native-provider', async span => {
      try {
        const response = await fetch(...args);
        const error = new Error(`Native provider echoed ${secret}`);
        span.setStatus({ code: 2, message: error.message });
        span.recordException(error);
        return response;
      } finally { span.end(); }
    });
    model = createOpenAI({ apiKey: secret, ...(baseURL ? { baseURL } : {}), ...(scenario.startsWith('secret-native') ? { fetch: fetchWithNativeSpan } : {}) }).chat('gpt-4o-mini');
  } else {
    const { createMockModel } = await load('@mastra/core/test-utils/llm-mock');
    model = createMockModel({ mockText: 'Answer.' });
  }
  const agent = new Agent({ id: 'worker', name: 'worker', instructions: 'Answer briefly.', model });
  const mastra = new Mastra({ logger: false, agents: { agent }, observability });
  let output;
  const parameters = { temperature: 0.25, topK: 7, stopSequences: ['private-stop-value'] };
  if (embeddingScenario) {
    for (let i = 0; i < embeddingOutputs.length; i++) {
      const span = observability.getDefaultInstance().startSpan({ name: `embedding-${i}`, type: SpanType.RAG_EMBEDDING,
        input: ['public input'], attributes: { model: 'mock-embedding' } });
      span.end({ output: embeddingOutputs[i] });
    }
    output = 'embeddings';
    assert.equal(requests.length, 0, 'direct real Mastra spans make no provider requests');
  } else if (scenario.startsWith('params-direct')) {
    const span = observability.getDefaultInstance().startSpan({ name: 'direct-model', type: SpanType.MODEL_GENERATION,
      attributes: { model: 'mock', provider: 'mock', parameters } });
    span.end();
    output = 'direct';
  } else {
    const generate = async () => {
      try { output = (await mastra.getAgent('agent').generate('Answer.', { modelSettings: parameters, maxRetries: 0 })).text; }
      catch (error) {
        output = error.message;
        if (outerCredential) {
          const transaction = trace.getActiveSpan();
          assert.equal(transaction.isRecording(), true, 'enclosing application transaction is a real SDK span');
          transaction.setStatus({ code: 2, message: error.message });
          transaction.recordException(error);
          transaction.setAttribute('app.error', error.message);
          assert.equal(transaction.status.message, error.message, 'the original application span is not mutated');
        }
      }
    };
    if (outerCredential) {
      await trace.getTracer('application').startActiveSpan('application-transaction', async transaction => {
        try { await generate(); }
        finally { transaction.end(); }
      });
      // The registry must also protect unrelated roots after Mastra execution.
      const later = trace.getTracer('application').startSpan('later-unrelated-transaction');
      later.setStatus({ code: 2, message: `Later provider echo: ${secret}` });
      later.addEvent(`Later echo: ${secret}`, { 'app.echo': secret });
      later.end();
    } else await generate();
    if (credentialScenario) {
      assert.equal(requests.length, 1, 'real provider sends one request with no retries');
      assert(output.includes(secret), 'application error remains unchanged and unsanitized');
    } else assert.equal(output, 'Answer.');
  }
  await mastra.observability.shutdown();
  await globalThis[Symbol.for('traceai.auto.agent.v1')].processor.forceFlush();
  const wire = spans.filter(span => span.attributes['traceai.capture.layer'] === 'wire');
  if (embeddingScenario) {
    const embeddings = spans.filter(span => span.attributes['gen_ai.span.kind'] === 'EMBEDDING');
    assert.equal(embeddings.length, embeddingOutputs.length, 'every embedding identity must survive privacy filtering');
    for (let i = 0; i < embeddingOutputs.length; i++) {
      const original = appEvents.find(event => event.type === 'span_ended' && event.exportedSpan.name === `embedding-${i}`);
      assert.deepEqual(original.exportedSpan.output, embeddingOutputs[i], 'application exporter output is not rewritten');
      const captured = embeddings.find(span => span.name === `embedding-${i}`);
      assert(captured, 'embedding span retained');
      if (visible) {
        assert.deepEqual(JSON.parse(captured.attributes['output.value']), { embeddings: embeddingOutputs[i] }, 'default capture labels and preserves all vector output');
      } else {
        assert(!JSON.stringify({ attributes: captured.attributes, events: captured.events, status: captured.status }).includes('0.12345'), 'configured embedding/output hiding must not leak numeric vectors');
        assert.equal(captured.attributes['output.value'], hideEmbeddingOutput ? '__REDACTED__' : '[REDACTED]');
      }
    }
  } else if (credentialScenario) {
    assert.equal(wire.length, baseline ? 1 : 0, 'coverage does not need duplicate wire export for privacy');
    const checked = spans;
    assert(!JSON.stringify(checked.map(span => ({ name: span.name, attributes: span.attributes, status: span.status, events: span.events,
      links: span.links, resource: span.resource.attributes, scope: span.instrumentationScope }))).includes(secret), 'no provider credential anywhere exported, including application spans');
    if (outerCredential) {
      for (const name of ['application-transaction', 'later-unrelated-transaction']) {
        const captured = spans.find(span => span.name === name);
        assert(captured, `${name} actually exported`);
        assert(captured.status.message.includes('<redacted>'), 'existing and unrelated transaction status redacted by the core registry');
        assert(captured.events.length > 0, 'application exception/event actually exported');
        assert(JSON.stringify(captured.events).includes('<redacted>'), 'application exception/event redacted');
      }
    }
    if (scenario.startsWith('secret-native')) assert(spans.some(span => span.name === 'native-provider' && span.events.some(event => event.name === 'exception')), 'native SDK span and its exception were actually exported');
    if (!baseline) {
      const app = appEvents.find(event => event.type === 'span_ended' && event.exportedSpan.type === SpanType.AGENT_RUN);
      assert(app, 'application exporter still receives original events');
      assert(JSON.stringify(app).includes(secret), 'FI privacy must not mutate another exporter’s event');
      const errors = spans.filter(span => span.attributes['error.message'] || span.events.some(event => event.name === 'exception'));
      assert(errors.length > 0, 'actually exported error spans');
      assert(JSON.stringify(errors.map(span => ({ attributes: span.attributes, status: span.status, events: span.events }))).includes('<redacted>'), 'credential is redacted rather than relying on missing spans');
    }
  } else {
    const models = spans.filter(span => span.attributes['gen_ai.span.kind'] === 'LLM');
    assert(models.length > 0, 'real model spans exported');
    if (visible) {
      assert(models.some(span => span.attributes['gen_ai.request.parameters']?.includes('private-stop-value')), 'canonical parameters remain available when not hidden');
      assert(models.some(span => JSON.parse(span.attributes['gen_ai.request.parameters'] ?? '{}').top_k === 7));
    } else {
      for (const span of models) {
        assert.equal(span.attributes['gen_ai.request.top_k'], undefined);
        assert.equal(span.attributes['gen_ai.request.stop_sequences'], undefined);
        assert.equal(span.attributes['gen_ai.request.temperature'], undefined);
        assert.equal(span.attributes['gen_ai.request.parameters'], undefined);
      }
      assert(!JSON.stringify(models.map(span => span.attributes)).includes('private-stop-value'));
    }
  }
  await auto.shutdown();
  console.log(JSON.stringify({ ok: true, output, requests, wireCount: wire.length, total: spans.length }));
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  server?.closeAllConnections();
  server?.close();
});
