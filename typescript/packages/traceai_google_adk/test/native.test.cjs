const assert = require('node:assert/strict');
const { test } = require('node:test');
const { GoogleADKSpanExporter, configure } = require('../dist/src');
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');

function collect(options) {
  const spans = [];
  const delegate = {export(batch, done) { spans.push(...batch); done({code: 0}); }, shutdown: async () => {}, forceFlush: async () => {}};
  const exporter = new GoogleADKSpanExporter(delegate, options);
  const provider = new BasicTracerProvider({spanProcessors: [new SimpleSpanProcessor(exporter)]});
  return {spans, exporter, provider, tracer: provider.getTracer('gcp.vertex.agent')};
}

const sensitive = {
  'gen_ai.request.model': 'gemini-test',
  'gen_ai.usage.input_tokens': 11,
  'gen_ai.usage.output_tokens': 7,
  'gcp.vertex.agent.llm_request': JSON.stringify({contents: [{role: 'user', parts: [{text: 'PRIVATE_INPUT'}]}]}),
  'gcp.vertex.agent.llm_response': JSON.stringify({content: {parts: [{text: 'PRIVATE_OUTPUT'}]}, usageMetadata: {promptTokenCount: 11, candidatesTokenCount: 7, thoughtsTokenCount: 5, totalTokenCount: 23}}),
  'gen_ai.conversation.id': 'PRIVATE_SESSION',
  'gen_ai.agent.description': 'PRIVATE_DESCRIPTION',
  'gcp.vertex.agent.future_content_field': 'PRIVATE_FUTURE',
};

test('default privacy removes native payloads, sessions, errors and future attributes before export', async () => {
  const c = collect();
  const original = c.tracer.startSpan('call_llm', {attributes: sensitive, links: [{context: {traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1}, attributes: {private: 'PRIVATE_LINK'}}]});
  original.setStatus({code: 2, message: 'PRIVATE_ERROR'});
  original.recordException(new Error('PRIVATE_ERROR'));
  original.addEvent('PRIVATE_EVENT', {private: 'PRIVATE_EVENT_ATTRIBUTE'});
  original.end();
  await c.provider.forceFlush();
  const span = c.spans[0];
  assert.equal(span.attributes['fi.span.kind'], 'LLM');
  assert.equal(span.attributes['gen_ai.usage.output_tokens'], 12);
  assert.equal(span.attributes['gen_ai.usage.output_tokens.reasoning'], 5);
  assert.equal(span.attributes['gen_ai.usage.total_tokens'], 23);
  assert.equal(span.status.code, 2);
  assert.equal(span.status.message, undefined);
  assert(!JSON.stringify({attributes: span.attributes, status: span.status, events: span.events, links: span.links}).includes('PRIVATE_'));
  assert.equal(original.attributes['gcp.vertex.agent.llm_request'], sensitive['gcp.vertex.agent.llm_request']);
  assert.equal(original.status.message, 'PRIVATE_ERROR');
  assert.equal(span.spanContext().spanId, original.spanContext().spanId);
  await c.provider.shutdown();
});

test('explicit content and session opt-in maps native input, output and conversation', async () => {
  const c = collect({captureContent: true, captureSessionId: true});
  c.tracer.startSpan('call_llm', {attributes: sensitive}).end();
  await c.provider.forceFlush();
  const a = c.spans[0].attributes;
  assert.equal(a['input.value'], sensitive['gcp.vertex.agent.llm_request']);
  assert.equal(a['output.value'], sensitive['gcp.vertex.agent.llm_response']);
  assert.equal(a['session.id'], 'PRIVATE_SESSION');
  assert.equal(a['gen_ai.conversation.id'], 'PRIVATE_SESSION');
  assert.equal(a['input.mime_type'], 'application/json');
  assert.equal(a['gcp.vertex.agent.llm_request'], undefined);
  await c.provider.shutdown();
});

test('agent, workflow, tool and legacy-native names classify without extra spans', async () => {
  const c = collect({captureContent: true, captureSessionId: true});
  const cases = [
    ['invoke_agent agent', {'gen_ai.operation.name': 'invoke_agent'}, 'AGENT'],
    ['agent_run [agent]', {}, 'AGENT'],
    ['invocation', {}, 'CHAIN'],
    ['workflow', {'gen_ai.operation.name': 'invoke_workflow'}, 'CHAIN'],
    ['node', {'gen_ai.operation.name': 'execute_node'}, 'CHAIN'],
    ['tool', {'gen_ai.operation.name': 'execute_tool', 'gcp.vertex.agent.tool_call_args': '{"x":1}', 'gcp.vertex.agent.tool_response': '{"result":2}', 'gcp.vertex.agent.session_id': 'session'}, 'TOOL'],
  ];
  cases.forEach(([name, attributes]) => c.tracer.startSpan(name, {attributes}).end());
  await c.provider.forceFlush();
  assert.deepEqual(c.spans.map(s => s.attributes['fi.span.kind']), cases.map(c => c[2]));
  assert.equal(c.spans.at(-1).attributes['input.value'], '{"x":1}');
  assert.equal(c.spans.at(-1).attributes['output.value'], '{"result":2}');
  assert.equal(c.spans.at(-1).attributes['session.id'], 'session');
  await c.provider.shutdown();
});

test('absent, malformed or invalid usage never invents totals or crashes', async () => {
  const c = collect();
  for (const response of ['{}', 'bad json', 'null', '{"usageMetadata":{"thoughtsTokenCount":-1}}']) {
    c.tracer.startSpan('call_llm', {attributes: {'gen_ai.usage.output_tokens': 7, 'gcp.vertex.agent.llm_response': response}}).end();
  }
  await c.provider.forceFlush();
  for (const span of c.spans) {
    assert.equal(span.attributes['gen_ai.usage.output_tokens'], 7);
    assert.equal(span.attributes['gen_ai.usage.total_tokens'], undefined);
    assert.equal(span.attributes['gen_ai.usage.output_tokens.reasoning'], undefined);
  }
  await c.provider.shutdown();
});

test('non-ADK spans pass through unchanged and exporter lifecycle is forwarded', async () => {
  let flushed = 0, stopped = 0, received;
  const exporter = new GoogleADKSpanExporter({export(spans, done) {received = spans[0]; done({code: 0});}, forceFlush: async () => {flushed++;}, shutdown: async () => {stopped++;}});
  const other = {instrumentationScope: {name: 'other'}};
  exporter.export([other], result => assert.equal(result.code, 0));
  assert.equal(received, other);
  await exporter.forceFlush(); await exporter.shutdown();
  assert.equal(flushed, 1); assert.equal(stopped, 1);
});

test('configuration validates inputs and refuses to replace an existing provider', async () => {
  const options = {projectName: 'synthetic', apiKey: 'synthetic', secretKey: 'synthetic'};
  assert.throws(() => configure({...options, projectName: ''}), /projectName/);
  assert.throws(() => configure({...options, apiKey: ''}), /FI_API_KEY/);
  assert.throws(() => configure({...options, endpoint: 'http://collector.example/traces'}), /HTTPS/);
  assert.throws(() => configure({...options, endpoint: 'https://user:password@collector.example/traces'}), /credentials/);
  const existing = new BasicTracerProvider();
  assert.equal(trace.setGlobalTracerProvider(existing), true);
  assert.throws(() => configure({...options, endpoint: 'http://127.0.0.1:9/v1/traces'}), /already registered/);
  assert.equal(trace.getTracerProvider().getDelegate(), existing);
  await existing.shutdown();
  trace.disable();
});
