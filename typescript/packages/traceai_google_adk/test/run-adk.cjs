// Run with ADK_RUNTIME=/absolute/path/to/an/isolated/npm/install.
// Synthetic model and localhost collector only. No model/provider API requests.
const assert = require('node:assert/strict');
const http = require('node:http');
const { createRequire } = require('node:module');
const { once } = require('node:events');
const { configure, GoogleADKInstrumentation, isPatched } = require(process.env.TRACEAI_PACKAGE || '../dist/src');
const { trace } = require('@opentelemetry/api');
const adkRequire = createRequire(process.env.ADK_RUNTIME + '/package.json');

async function main() {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => { requests.push({url: req.url, headers: req.headers, body: JSON.parse(Buffer.concat(chunks).toString())}); res.writeHead(200, {'content-type': 'application/json'}); res.end('{}'); });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const captureContent = process.env.CAPTURE_CONTENT === '1';
  const options = {projectName: 'offline-adk', endpoint: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`, apiKey: 'synthetic-api-key', secretKey: 'synthetic-secret-key', captureContent, captureSessionId: captureContent};
  const provider = configure(options);
  try {
    assert.throws(() => configure(options), /already configured/);
    const adk = adkRequire('@google/adk');
    const original = adk.BaseAgent.prototype.runAsync;
    // Old entry point must not install duplicate wrappers after native setup.
    const legacy = new GoogleADKInstrumentation();
    legacy.manuallyInstrument(adk);
    assert.equal(isPatched(), false);
    assert.equal(adk.BaseAgent.prototype.runAsync, original);
    const version = JSON.parse(require('node:fs').readFileSync(process.env.ADK_RUNTIME + '/node_modules/@google/adk/package.json', 'utf8')).version;
    let agent;
    if (version === '0.1.0') {
      class OfflineAgent extends adk.BaseAgent { async *runAsyncImpl() {} }
      agent = new OfflineAgent({name: 'offline_agent'});
    } else {
      class OfflineModel extends adk.BaseLlm {
        constructor() { super({model: 'gemini-offline'}); this.calls = 0; }
        async *generateContentAsync() {
          this.calls++;
          yield {
            content: {role: 'model', parts: this.calls === 1 ? [{functionCall: {id: 'call1', name: 'echo', args: {value: 'PRIVATE_TOOL'}}}] : [{text: 'PRIVATE_OUTPUT'}]},
            usageMetadata: {promptTokenCount: 11, candidatesTokenCount: 7, thoughtsTokenCount: 5, totalTokenCount: 23},
          };
        }
      }
      const tool = new adk.FunctionTool({name: 'echo', description: 'Echo synthetic data', execute: async ({value}) => ({result: value})});
      agent = new adk.LlmAgent({name: 'offline_agent', model: new OfflineModel(), tools: [tool]});
    }
    const runner = new adk.InMemoryRunner({agent, appName: 'offline'});
    await runner.sessionService.createSession({appName: 'offline', userId: 'offline-user', sessionId: 'PRIVATE_SESSION'});
    const events = [];
    for await (const event of runner.runAsync({userId: 'offline-user', sessionId: 'PRIVATE_SESSION', newMessage: {role: 'user', parts: [{text: 'PRIVATE_INPUT'}]}})) events.push(event);
    await provider.forceFlush();
    legacy.disable();
    const spans = requests.flatMap(r => r.body.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    const attrs = span => Object.fromEntries(span.attributes.map(a => [a.key, a.value.stringValue ?? Number(a.value.intValue ?? a.value.doubleValue)]));
    const kinds = spans.map(span => attrs(span)['fi.span.kind']);
    assert.equal(kinds.filter(kind => kind === 'AGENT').length, 1);
    assert.equal(kinds.filter(kind => kind === 'CHAIN').length, 1);
    for (const request of requests) {
      assert.equal(request.url, '/tracer/v1/traces');
      assert.equal(request.headers['x-api-key'], 'synthetic-api-key');
      assert.equal(request.headers['x-secret-key'], 'synthetic-secret-key');
      const resources = Object.fromEntries(request.body.resourceSpans[0].resource.attributes.map(a => [a.key, a.value.stringValue]));
      assert.equal(resources.project_name, 'offline-adk');
      assert.equal(resources.project_type, 'observe');
    }
    if (!captureContent) assert(!JSON.stringify(requests.map(r => r.body)).includes('PRIVATE_'));
    if (version !== '0.1.0') {
      assert.equal(new Set(spans.map(span => span.traceId)).size, 1);
      assert.equal(kinds.filter(kind => kind === 'LLM').length, 2);
      assert.equal(kinds.filter(kind => kind === 'TOOL').length, 1);
      for (const span of spans.filter(span => attrs(span)['fi.span.kind'] === 'LLM')) {
        assert.equal(attrs(span)['gen_ai.usage.output_tokens'], 12);
        assert.equal(attrs(span)['gen_ai.usage.total_tokens'], 23);
        assert.equal(attrs(span)['gen_ai.usage.output_tokens.reasoning'], 5);
        assert.equal(Boolean(attrs(span)['input.value']), captureContent);
        assert.equal(Boolean(attrs(span)['session.id']), captureContent);
      }
      const root = spans.find(span => span.name === 'invocation');
      const agentSpan = spans.find(span => attrs(span)['fi.span.kind'] === 'AGENT');
      assert.equal(agentSpan.parentSpanId, root.spanId);
      assert(events.some(event => event.content?.parts?.some(part => part.text === 'PRIVATE_OUTPUT')));
    }
    assert.equal(adk.BaseAgent.prototype.runAsync, original);
    assert.equal(trace.getActiveSpan(), undefined);
    requests.length = 0;
    const failedSpan = provider.getTracer('gcp.vertex.agent').startSpan('call_llm');
    failedSpan.setStatus({code: 2, message: 'PRIVATE_ERROR'});
    failedSpan.recordException(new Error('PRIVATE_ERROR'));
    failedSpan.addEvent('PRIVATE_EVENT', {payload: 'PRIVATE_EVENT_CONTENT'});
    failedSpan.end();
    await provider.forceFlush();
    const errorSpans = requests.flatMap(r => r.body.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    assert.equal(errorSpans.length, 1);
    assert.equal(errorSpans[0].status.code, 2);
    assert(!JSON.stringify(requests.map(r => r.body)).includes('PRIVATE_'));
    console.log(JSON.stringify({version, captureContent, spanCount: spans.length, kinds, traceCount: new Set(spans.map(span => span.traceId)).size, nativeMethodsUnchanged: true, wireChecks: 'passed', errorPrivacyWireCheck: 'passed'}));
  } finally {
    await provider.shutdown();
    server.close();
    await once(server, 'close');
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
