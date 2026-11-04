'use strict';
// Real ADK 2.4, Gemini and GenAI HTTP/SSE clients. Only the provider endpoint
// is replaced by localhost. Also decode the adapter's real OTLP/HTTP export.
// Usage: node native-http.cjs <package-path> <isolated-adk-runtime> <mode>
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const [packagePath, runtimePath, mode = 'default'] = process.argv.slice(2);
const pr = createRequire(packagePath + '/package.json');
const ar = createRequire(runtimePath + '/package.json');
const { NodeTracerProvider } = pr('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = pr('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = pr('@opentelemetry/exporter-trace-otlp-http');
const { trace } = pr('@opentelemetry/api');
const { GoogleADKSpanExporter, recordGoogleADKModelError } = pr(packagePath);
const toolError = mode.startsWith('tool-error');
const providerError = mode.startsWith('provider-error');
const streaming = mode === 'stream' || mode === 'provider-error-stream';
const usage = { promptTokenCount: 100, cachedContentTokenCount: 60, candidatesTokenCount: 10, thoughtsTokenCount: 5, totalTokenCount: 115 };
let calls = 0;
const requests = [], batches = [];
const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', chunk => chunks.push(chunk));
  req.on('end', () => {
    const body = JSON.parse(Buffer.concat(chunks).toString());
    if (req.url === '/v1/traces') {
      batches.push(body);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{}');
      return;
    }
    calls++;
    requests.push({ path: req.url, body });
    if (providerError) {
      res.writeHead(400, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { code: 400, message: 'PRIVATE_PROVIDER_ERROR', status: 'INVALID_ARGUMENT' } }));
      return;
    }
    const response = (text, done = true) => ({
      candidates: [{ index: 0, content: { role: 'model', parts: toolError && calls === 1
        ? [{ functionCall: { id: 'call-1', name: 'echo', args: { value: 'PRIVATE_TOOL' } } }]
        : [{ text }] }, ...(done ? { finishReason: 'STOP' } : {}) }],
      usageMetadata: usage, modelVersion: 'gemini-2.5-flash-001',
    });
    if (req.url.includes('streamGenerateContent')) {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: ' + JSON.stringify(response('PRIVATE_PART_A', false)) + '\n\n');
      res.end('data: ' + JSON.stringify(response('PRIVATE_PART_B')) + '\n\n');
    } else {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(response('PRIVATE_OUTPUT')));
    }
  });
});
const attrs = span => Object.fromEntries(span.attributes.map(a => [a.key, a.value.stringValue ?? Number(a.value.intValue ?? a.value.doubleValue)]));
async function main() {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  // Check both the explicit flag and the stricter native-route default.
  if (mode === 'tool-error-hide') process.env.FI_HIDE_ERROR_DETAILS = 'true';
  else delete process.env.FI_HIDE_ERROR_DETAILS;
  const raw = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(raw),
    new SimpleSpanProcessor(new GoogleADKSpanExporter(new OTLPTraceExporter({ url: origin + '/v1/traces' }), { captureContent: toolError })),
  ] });
  provider.register();
  try {
    process.env.GOOGLE_GENAI_API_KEY = 'SYNTHETIC_GOOGLE_KEY';
    const adk = ar('@google/adk');
    const { GoogleGenAI } = ar('@google/genai');
    assert.equal(typeof recordGoogleADKModelError, 'function');
    class ErrorTelemetry extends adk.BasePlugin {
      constructor() { super('traceai_model_errors'); }
      onModelErrorCallback = recordGoogleADKModelError;
    }
    const originalRun = adk.BaseAgent.prototype.runAsync;
    const tool = new adk.FunctionTool({ name: 'echo', description: 'Synthetic tool', execute: async () => { throw new Error('PRIVATE_TOOL_ERROR'); } });
    const agent = new adk.LlmAgent({ name: 'http_agent', model: 'gemini-2.5-flash', tools: toolError ? [tool] : [] });
    // Redirect only the transport, not model.generateContentAsync or Runner.
    const model = agent.canonicalModel;
    agent.model = model;
    model._apiClient = new GoogleGenAI({ apiKey: 'SYNTHETIC_GOOGLE_KEY', vertexai: false,
      httpOptions: { baseUrl: origin, apiVersion: 'v1beta', retryOptions: { attempts: 1 } } });
    const runner = new adk.InMemoryRunner({ agent, appName: 'http-contract', plugins: [new ErrorTelemetry()] });
    await runner.sessionService.createSession({ appName: 'http-contract', userId: 'synthetic-user', sessionId: 'PRIVATE_SESSION' });
    const events = [];
    for await (const event of runner.runAsync({ userId: 'synthetic-user', sessionId: 'PRIVATE_SESSION',
      newMessage: { role: 'user', parts: [{ text: 'PRIVATE_INPUT' }] },
      runConfig: streaming ? { streamingMode: adk.StreamingMode.SSE } : undefined })) events.push(event);
    await provider.forceFlush();
    const spans = batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    const llms = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
    assert(calls > 0, 'real GenAI HTTP client reached localhost');
    assert(requests.every(r => r.path.includes('gemini-2.5-flash')));
    assert.equal(spans.length, raw.getFinishedSpans().length, 'no duplicate spans');
    assert.equal(new Set(spans.map(s => s.spanId)).size, spans.length);
    assert.equal(new Set(spans.map(s => s.traceId)).size, 1);
    assert.deepEqual(new Set(spans.map(s => s.spanId)), new Set(raw.getFinishedSpans().map(s => s.spanContext().spanId)));
    const root = spans.find(s => s.name === 'invocation');
    const agentSpan = spans.find(s => attrs(s)['fi.span.kind'] === 'AGENT');
    assert.equal(agentSpan.parentSpanId, root.spanId);
    assert(llms.every(s => s.parentSpanId === agentSpan.spanId));
    assert.equal(adk.BaseAgent.prototype.runAsync, originalRun);
    assert.equal(trace.getActiveSpan(), undefined);
    const exported = JSON.stringify(batches);
    assert(!exported.includes('SYNTHETIC_GOOGLE_KEY'));
    assert(!exported.includes('PRIVATE_TOOL_ERROR'));
    assert(!exported.includes('PRIVATE_PROVIDER_ERROR'));
    if (!toolError) assert(!exported.includes('PRIVATE_'), 'default privacy');
    if (providerError) {
      assert.equal(llms.length, 1);
      const llm = llms[0];
      assert.equal(llm.status.code, 2, 'real provider failure marks the existing native LLM span ERROR');
      assert.equal(llm.status.message, undefined);
      assert.equal(attrs(llm)['gen_ai.request.model'], 'gemini-2.5-flash');
      assert.equal(attrs(llm)['gen_ai.system'], 'gcp.vertex.agent');
      assert.equal(attrs(llm)['error.type'], 'ApiError');
      assert.equal(attrs(llm)['http.response.status_code'], 400);
      assert.equal(attrs(llm)['gen_ai.usage.input_tokens'], undefined, 'no fabricated usage on failure');
      assert(events.some(e => e.errorCode === '400' && e.errorMessage === 'PRIVATE_PROVIDER_ERROR'), 'application error semantics unchanged');
    } else {
      assert.equal(llms.length, toolError ? 2 : 1);
      for (const span of llms) {
        assert.equal(attrs(span)['gen_ai.usage.input_tokens'], 100);
        assert.equal(attrs(span)['gen_ai.usage.cache_read_tokens'], 60);
        assert.equal(attrs(span)['gen_ai.usage.output_tokens'], 15);
        assert.equal(attrs(span)['gen_ai.usage.total_tokens'], 115);
        assert.notEqual(span.status.code, 2, 'successful calls are not marked as failures');
      }
      if (streaming) assert(events.some(e => e.content?.parts?.some(p => p.text?.includes('PRIVATE_PART_B'))));
      if (toolError) {
        assert.equal(calls, 2);
        assert(raw.getFinishedSpans().some(s => s.attributes['gcp.vertex.agent.llm_request']?.includes('PRIVATE_TOOL_ERROR')), 'ADK copied the real tool failure into later history');
        assert(llms.some(s => attrs(s)['input.value']?.includes('__REDACTED__')));
        assert(spans.some(s => attrs(s)['fi.span.kind'] === 'TOOL' && s.status.code === 2));
        assert(exported.includes('PRIVATE_INPUT'), 'content opt-in is active, not blanket hidden');
      }
    }
    console.log(JSON.stringify({ mode, result: 'PASS', calls, spanCount: spans.length, realHttpAndOtlp: true }));
  } finally {
    await provider.shutdown();
    server.closeAllConnections();
    server.close();
    await once(server, 'close');
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
