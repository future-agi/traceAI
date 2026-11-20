// Native ESM + loopback OTLP check for the released VoltAgent/AI SDK dependency pair.
// Run with NODE_ENV=production to prevent VoltAgent's optional models.dev refresh.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { Agent, VoltAgent, VoltAgentObservability } from '@voltagent/core';
import { MockLanguageModelV3 } from 'ai/test';
import { ProjectType, register } from '@traceai/fi-core';
import { FIVoltAgentSpanProcessor } from '../dist/esm/index.js';

const received = [];
const server = createServer((request, response) => {
  const parts = [];
  request.on('data', part => parts.push(part));
  request.on('end', () => {
    received.push({ path: request.url, type: request.headers['content-type'], body: Buffer.concat(parts) });
    response.writeHead(200);
    response.end();
  });
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));

let app;
let processor;
let provider;
try {
  provider = register({
    projectName: 'voltagent-local-test', projectType: ProjectType.OBSERVE,
    endpoint: `http://127.0.0.1:${server.address().port}`,
    headers: { 'x-api-key': 'synthetic', 'x-secret-key': 'synthetic' },
    setGlobalTracerProvider: false,
  });
  processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider, batch: false });
  const observability = new VoltAgentObservability({ spanProcessors: [processor] });
  const model = new MockLanguageModelV3({
    provider: 'mock-provider', modelId: 'mockai/mock-model-1',
    doGenerate: async () => ({
      content: [{ type: 'text', text: 'native ESM works' }],
      finishReason: { unified: 'stop', raw: 'stop' },
      usage: {
        inputTokens: { total: 2, noCache: 2 },
        outputTokens: { total: 3, text: 3 },
      },
      warnings: [],
    }),
  });
  const agent = new Agent({ name: 'assistant', instructions: 'test', model });
  app = new VoltAgent({ agents: { assistant: agent }, observability, checkDependencies: false });
  assert.equal((await agent.generateText('hello')).text, 'native ESM works');
  await processor.forceFlush();
  assert.ok(received.length > 0, 'expected an OTLP export');
  assert.ok(received.every(item => item.path === '/tracer/v1/traces'));
  assert.ok(received.every(item => item.type?.includes('application/json')));
  const spans = received.flatMap(item => JSON.parse(item.body.toString('utf8')).resourceSpans ?? [])
    .flatMap(resource => resource.scopeSpans ?? [])
    .flatMap(scope => scope.spans ?? []);
  const modelSpan = spans.find(span => span.name === 'llm:generateText');
  assert.ok(modelSpan, 'expected mapped LLM span in OTLP payload');
  const attributes = Object.fromEntries(modelSpan.attributes.map(({ key, value }) => [key, value]));
  assert.equal(attributes['fi.span.kind']?.stringValue, 'LLM');
  assert.equal(attributes['gen_ai.request.model']?.stringValue, 'mockai/mock-model-1');
  assert.equal(Number(attributes['gen_ai.usage.input_tokens']?.intValue), 2);
  assert.equal(Number(attributes['gen_ai.usage.output_tokens']?.intValue), 3);
  console.log(`Native VoltAgent agent exported ${received.length} OTLP request(s) with mapped LLM attributes`);
} finally {
  if (app) await app.shutdown();
  if (processor) await processor.shutdown();
  if (provider) await provider.shutdown();
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}
