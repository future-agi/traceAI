'use strict';
// Real ADK 2.4, Gemini and GenAI HTTP/SSE clients. Only the provider endpoint
// is replaced by localhost. Also decode the adapter's real OTLP/HTTP export.
// Usage: node native-http.cjs <package-path> <isolated-adk-runtime> <mode> [privacy-flag]
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const [packagePath, runtimePath, mode = 'default', privacyFlag] = process.argv.slice(2);
const pr = createRequire(packagePath + '/package.json');
const ar = createRequire(runtimePath + '/package.json');
const { NodeTracerProvider } = pr('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = pr('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = pr('@opentelemetry/exporter-trace-otlp-http');
const { trace } = pr('@opentelemetry/api');
const { GoogleADKSpanExporter, recordGoogleADKModelError } = pr(packagePath);
const toolError = mode.startsWith('tool-error');
const toolUsage = mode.startsWith('tool-usage');
const providerError = mode.startsWith('provider-error');
const streaming = mode.includes('stream');
const outputModality = mode.startsWith('audio-output') ? 'AUDIO' : mode.startsWith('image-output') ? 'IMAGE' : undefined;
const audio = mode.startsWith('audio') && !outputModality;
const captureContent = toolError || mode.endsWith('-capture');
const audioData = Buffer.from('SYNTHETIC_AUDIO').toString('base64');
const outputData = Buffer.from('SYNTHETIC_OUTPUT_MEDIA').toString('base64');
const modelName = outputModality === 'AUDIO' ? 'gemini-2.5-flash-preview-tts' : outputModality === 'IMAGE' ? 'gemini-2.5-flash-image' : 'gemini-2.5-flash';
const usage = toolUsage
  ? { promptTokenCount: 100, cachedContentTokenCount: 40, toolUsePromptTokenCount: 300,
      candidatesTokenCount: 20, thoughtsTokenCount: 10, totalTokenCount: 430,
      promptTokensDetails: [{ modality: 'TEXT', tokenCount: 100 }],
      toolUsePromptTokensDetails: [{ modality: 'TEXT', tokenCount: 300 }] }
  : outputModality
  ? { promptTokenCount: 100, cachedContentTokenCount: 40, candidatesTokenCount: 14, thoughtsTokenCount: 4, totalTokenCount: 118,
      candidatesTokensDetails: [{ modality: outputModality, tokenCount: 6 }, { modality: outputModality, tokenCount: 8 }] }
  : audio
  ? { promptTokenCount: 100, cachedContentTokenCount: 40, candidatesTokenCount: 10, thoughtsTokenCount: 4, totalTokenCount: 114,
      promptTokensDetails: [{ modality: 'TEXT', tokenCount: 20 }, { modality: 'AUDIO', tokenCount: 30 }, { modality: 'AUDIO', tokenCount: 50 }] }
  : { promptTokenCount: 100, cachedContentTokenCount: 60, candidatesTokenCount: 10, thoughtsTokenCount: 5, totalTokenCount: 115 };
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
        : outputModality ? [{ inlineData: { mimeType: outputModality === 'AUDIO' ? 'audio/pcm' : 'image/png', data: outputData } }]
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
  if (privacyFlag) {
    assert(['FI_HIDE_INPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_OUTPUTS', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_OUTPUT_TEXT', 'FI_PII_REDACTION'].includes(privacyFlag));
    process.env[privacyFlag] = 'true';
  }
  const raw = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(raw),
    new SimpleSpanProcessor(new GoogleADKSpanExporter(new OTLPTraceExporter({ url: origin + '/v1/traces' }), { captureContent })),
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
    const agent = new adk.LlmAgent({ name: 'http_agent', model: modelName, tools: toolError ? [tool] : toolUsage ? [adk.GOOGLE_SEARCH] : [],
      ...(outputModality ? { generateContentConfig: { responseModalities: outputModality === 'AUDIO' ? ['AUDIO'] : ['TEXT', 'IMAGE'] } } : {}) });
    // Redirect only the transport, not model.generateContentAsync or Runner.
    const model = agent.canonicalModel;
    agent.model = model;
    model._apiClient = new GoogleGenAI({ apiKey: 'SYNTHETIC_GOOGLE_KEY', vertexai: false,
      httpOptions: { baseUrl: origin, apiVersion: 'v1beta', retryOptions: { attempts: 1 } } });
    const runner = new adk.InMemoryRunner({ agent, appName: 'http-contract', plugins: [new ErrorTelemetry()] });
    await runner.sessionService.createSession({ appName: 'http-contract', userId: 'synthetic-user', sessionId: 'PRIVATE_SESSION' });
    const events = [];
    for await (const event of runner.runAsync({ userId: 'synthetic-user', sessionId: 'PRIVATE_SESSION',
      newMessage: { role: 'user', parts: [{ text: 'PRIVATE_INPUT' }, ...(audio ? [{ inlineData: { mimeType: 'audio/wav', data: audioData } }] : [])] },
      runConfig: streaming ? { streamingMode: adk.StreamingMode.SSE } : undefined })) events.push(event);
    await provider.forceFlush();
    const spans = batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    const llms = spans.filter(s => attrs(s)['fi.span.kind'] === 'LLM');
    assert(calls > 0, 'real GenAI HTTP client reached localhost');
    assert(requests.every(r => r.path.includes(modelName)));
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
    if (!captureContent) {
      assert(!exported.includes('PRIVATE_'), 'default privacy');
      assert(!exported.includes(audioData), 'audio payload stays private');
      assert(!exported.includes(outputData), 'output media stays private');
      assert(llms.every(s => attrs(s)['input.value'] === undefined && attrs(s)['output.value'] === undefined));
    }
    if (toolUsage) {
      assert.equal(calls, 1);
      assert(requests[0].body.tools.some(tool => tool.googleSearch), 'real GenAI client enabled built-in search');
      assert.equal(requests[0].path.includes('streamGenerateContent'), streaming);
      const native = raw.getFinishedSpans().find(s => s.name === 'call_llm');
      assert.deepEqual(JSON.parse(native.attributes['gcp.vertex.agent.llm_response']).usageMetadata, usage);
      assert(events.some(e => e.usageMetadata?.toolUsePromptTokenCount === 300));
      if (captureContent) {
        for (const direction of ['input', 'output']) {
          const hidden = privacyFlag === `FI_HIDE_${direction.toUpperCase()}S`
            || privacyFlag === `FI_HIDE_${direction.toUpperCase()}_MESSAGES`;
          const hiddenText = privacyFlag === `FI_HIDE_${direction.toUpperCase()}_TEXT`;
          const value = attrs(llms[0])[`${direction}.value`];
          if (hidden) assert.equal(value, '__REDACTED__');
          else if (hiddenText) assert(!value.includes('PRIVATE_'));
          else assert(value.includes('PRIVATE_'), 'content capture remains active');
        }
      }
      assert(llms.every(s => !Object.keys(attrs(s)).some(key => /cost|price/.test(key))), 'counts do not invent pricing');
    }
    if (audio) {
      assert(requests.some(r => r.body.contents.some(c => c.parts.some(p => p.inlineData?.data === audioData))), 'real GenAI client sent audio');
      const native = raw.getFinishedSpans().find(s => s.name === 'call_llm');
      assert.deepEqual(JSON.parse(native.attributes['gcp.vertex.agent.llm_response']).usageMetadata.promptTokensDetails,
        usage.promptTokensDetails, 'ADK retained the reported modality breakdown');
      if (captureContent) assert(exported.includes('PRIVATE_INPUT'), 'content opt-in is active');
    }
    if (outputModality) {
      assert(requests.some(r => r.body.generationConfig?.responseModalities?.includes(outputModality)), 'real GenAI client requested output modality');
      assert(events.some(e => e.content?.parts?.some(p => p.inlineData?.data === outputData)), 'real ADK emitted generated media');
      const native = raw.getFinishedSpans().find(s => s.name === 'call_llm');
      assert.deepEqual(JSON.parse(native.attributes['gcp.vertex.agent.llm_response']).usageMetadata.candidatesTokensDetails,
        usage.candidatesTokensDetails, 'ADK retained the reported output breakdown');
      if (captureContent) {
        assert(exported.includes('PRIVATE_INPUT'), 'content opt-in is active');
        if (privacyFlag === 'FI_HIDE_OUTPUTS' || privacyFlag === 'FI_HIDE_OUTPUT_MESSAGES') {
          assert(llms.every(s => attrs(s)['output.value'] === '__REDACTED__'));
        } else if (privacyFlag === 'FI_HIDE_OUTPUT_TEXT') {
          assert(!exported.includes(outputData));
          assert(llms.every(s => JSON.parse(attrs(s)['output.value']).usageMetadata.candidatesTokensDetails[0].modality === '__REDACTED__'));
        } else {
          assert(llms.every(s => JSON.parse(attrs(s)['output.value']).usageMetadata.candidatesTokensDetails[0].modality === outputModality));
        }
      }
    }
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
        assert.equal(attrs(span)['gen_ai.usage.input_tokens'], toolUsage ? 400 : 100,
          'input includes tool prompts once, not cache/modality subsets or repeated SSE summaries');
        if (toolUsage) {
          const wire = span.attributes.find(a => a.key === 'gen_ai.usage.input_tokens').value;
          assert(wire.intValue !== undefined || wire.doubleValue !== undefined, 'input usage is numeric on the wire');
        }
        assert.equal(attrs(span)['gen_ai.usage.cache_read_tokens'], usage.cachedContentTokenCount);
        assert.equal(attrs(span)['gen_ai.usage.output_tokens'], usage.candidatesTokenCount + usage.thoughtsTokenCount);
        assert.equal(attrs(span)['gen_ai.usage.total_tokens'], usage.totalTokenCount);
        assert.equal(attrs(span)['gen_ai.usage.input_tokens.audio'], audio ? 80 : undefined, 'reported audio usage survives content policy');
        for (const modality of ['AUDIO', 'IMAGE']) {
          const key = `gen_ai.usage.output_tokens.${modality.toLowerCase()}`;
          assert.equal(attrs(span)[key], modality === outputModality ? 14 : undefined, 'reported output modality usage survives content policy');
          if (modality === outputModality) {
            const wire = span.attributes.find(a => a.key === key).value;
            assert(wire.intValue !== undefined || wire.doubleValue !== undefined, 'usage is a numeric OTLP attribute');
          }
        }
        assert.notEqual(span.status.code, 2, 'successful calls are not marked as failures');
      }
      if (streaming && !outputModality) assert(events.some(e => e.content?.parts?.some(p => p.text?.includes('PRIVATE_PART_B'))));
      if (toolError) {
        assert.equal(calls, 2);
        assert(raw.getFinishedSpans().some(s => s.attributes['gcp.vertex.agent.llm_request']?.includes('PRIVATE_TOOL_ERROR')), 'ADK copied the real tool failure into later history');
        assert(llms.some(s => attrs(s)['input.value']?.includes('__REDACTED__')));
        assert(spans.some(s => attrs(s)['fi.span.kind'] === 'TOOL' && s.status.code === 2));
        assert(exported.includes('PRIVATE_INPUT'), 'content opt-in is active, not blanket hidden');
      }
    }
    console.log(JSON.stringify({ mode, privacyFlag, result: 'PASS', calls, spanCount: spans.length, realHttpAndOtlp: true,
      usage: llms.map(s => Object.fromEntries(Object.entries(attrs(s)).filter(([key]) => key.startsWith('gen_ai.usage.')))) }));
  } finally {
    await provider.shutdown();
    server.closeAllConnections();
    server.close();
    await once(server, 'close');
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
