const assert = require('node:assert/strict');
const { test } = require('node:test');
const { GoogleADKSpanExporter, configure } = require('../dist/src');
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');

function collect(options, spanLimits) {
  const spans = [];
  const delegate = {export(batch, done) { spans.push(...batch); done({code: 0}); }, shutdown: async () => {}, forceFlush: async () => {}};
  const exporter = new GoogleADKSpanExporter(delegate, options);
  const provider = new BasicTracerProvider({spanLimits, spanProcessors: [new SimpleSpanProcessor(exporter)]});
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

test('translated spans retain SDK getter-backed timing and dropped counts for downstream exporters', async () => {
  const c = collect(undefined, {attributeCountLimit: 2, eventCountLimit: 1, linkCountLimit: 1});
  try {
    const link = {context: {traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1}};
    const original = c.tracer.startSpan('call_llm', {
      startTime: [100, 0],
      attributes: sensitive,
      links: [link, link],
    });
    original.addEvent('exception', {'exception.type': 'Error'});
    original.addEvent('exception', {'exception.type': 'Error'});
    original.end([101, 123]);
    await c.provider.forceFlush();
    const span = c.spans[0];
    assert.deepEqual(span.duration, [1, 123]);
    assert.equal(span.ended, true);
    assert(original.droppedAttributesCount > 0);
    assert(original.droppedEventsCount > 0);
    for (const key of ['droppedAttributesCount', 'droppedEventsCount', 'droppedLinksCount']) {
      assert.equal(span[key], original[key], key);
    }
    for (const key of ['name', 'kind', 'parentSpanContext', 'startTime', 'endTime', 'resource', 'instrumentationScope']) {
      assert.deepEqual(span[key], original[key], key);
    }
    // ConsoleSpanExporter and other exporters read duration through this API.
    const { hrTimeToMicroseconds } = require('@opentelemetry/core');
    assert.equal(hrTimeToMicroseconds(span.duration), hrTimeToMicroseconds(original.duration));
  } finally {
    await c.provider.shutdown();
  }
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


for (const captureContent of [false, true]) {
  test(`audio input counts use only valid reported details (captureContent=${captureContent})`, async () => {
    const c = collect({captureContent});
    const audio = tokenCount => ({modality: 'AUDIO', tokenCount});
    const cases = [
      ['single audio count', [audio(80)], 80],
      ['aggregate audio only', [{modality: 'TEXT', tokenCount: 20}, audio(30), {modality: 'IMAGE', tokenCount: 10}, audio(50)], 80],
      ['reported zero', [audio(0)], 0],
      ['missing details', undefined, undefined],
      ['empty details', [], undefined],
      ['null details', null, undefined],
      ['non-array details', audio(80), undefined],
      ['string details', '[{"modality":"AUDIO","tokenCount":80}]', undefined],
      ['text only', [{modality: 'TEXT', tokenCount: 100}], undefined],
      ['unknown modality', [{modality: 'audio', tokenCount: 80}, {tokenCount: 80}], undefined],
      ['invalid counts', [audio(-1), audio('80'), audio(null), audio(true), audio(1.5), audio(Number.MAX_VALUE), audio(undefined)], undefined],
      ['invalid entries alongside valid audio', [null, 80, 'AUDIO', [], {}, audio(-1), audio('90'), audio(30), audio(50)], 80],
      ['unsafe sum', [audio(Number.MAX_SAFE_INTEGER), audio(1)], undefined],
    ];
    try {
      for (const [, promptTokensDetails] of cases) {
        c.tracer.startSpan('call_llm', {attributes: {
          'gcp.vertex.agent.llm_response': JSON.stringify({content: {parts: [{text: 'PRIVATE_OUTPUT'}]}, usageMetadata: {
            promptTokenCount: 100, cachedContentTokenCount: 40, candidatesTokenCount: 10, thoughtsTokenCount: 4,
            totalTokenCount: 114, promptTokensDetails,
          }}),
        }}).end();
      }
      // JSON permits numeric literals that parse to Infinity; never export them.
      c.tracer.startSpan('call_llm', {attributes: {
        'gcp.vertex.agent.llm_response': '{"usageMetadata":{"promptTokensDetails":[{"modality":"AUDIO","tokenCount":1e400}]}}',
      }}).end();
      // Audio detail alone must not fabricate aggregate or cache totals.
      c.tracer.startSpan('call_llm', {attributes: {
        'gcp.vertex.agent.llm_response': JSON.stringify({usageMetadata: {promptTokensDetails: [audio(80)]}}),
      }}).end();
      await c.provider.forceFlush();
      for (let i = 0; i < cases.length; i++) {
        const a = c.spans[i].attributes;
        assert.equal(a['gen_ai.usage.input_tokens.audio'], cases[i][2], cases[i][0]);
        assert.equal(a['gen_ai.usage.input_tokens'], 100);
        assert.equal(a['gen_ai.usage.cache_read_tokens'], 40);
        assert.equal(a['gen_ai.usage.output_tokens'], 14);
        assert.equal(a['gen_ai.usage.total_tokens'], 114);
        assert.equal(a['gcp.vertex.agent.llm_response'], undefined);
        assert.equal(a['output.value'] !== undefined, captureContent);
        if (!captureContent) assert(!JSON.stringify(a).includes('PRIVATE_OUTPUT'));
      }
      assert.equal(c.spans.at(-2).attributes['gen_ai.usage.input_tokens.audio'], undefined);
      const detailOnly = c.spans.at(-1).attributes;
      assert.equal(detailOnly['gen_ai.usage.input_tokens.audio'], 80);
      for (const key of ['input_tokens', 'cache_read_tokens', 'output_tokens', 'total_tokens']) {
        assert.equal(detailOnly[`gen_ai.usage.${key}`], undefined);
      }
    } finally { await c.provider.shutdown(); }
  });
}

for (const captureContent of [false, true]) {
  for (const modality of ['AUDIO', 'IMAGE']) {
    test(`${modality} output counts use only valid reported details (captureContent=${captureContent})`, async () => {
      const c = collect({captureContent});
      const detail = tokenCount => ({modality, tokenCount});
      const other = modality === 'AUDIO' ? 'IMAGE' : 'AUDIO';
      const key = `gen_ai.usage.output_tokens.${modality.toLowerCase()}`;
      const cases = [
        ['single count', [detail(14)], 14],
        ['aggregate only this modality', [{modality: 'TEXT', tokenCount: 2}, detail(6), {modality: other, tokenCount: 3}, detail(8)], 14],
        ['reported zero', [detail(0)], 0],
        ['missing details', undefined, undefined],
        ['empty details', [], undefined],
        ['null details', null, undefined],
        ['non-array details', detail(14), undefined],
        ['string details', JSON.stringify([detail(14)]), undefined],
        ['other modality only', [{modality: other, tokenCount: 14}], undefined],
        ['unknown modality', [{modality: modality.toLowerCase(), tokenCount: 14}, {tokenCount: 14}], undefined],
        ['invalid counts', [detail(-1), detail('14'), detail(null), detail(true), detail(1.5), detail(Number.MAX_VALUE), detail(undefined)], undefined],
        ['invalid entries alongside valid counts', [null, 14, modality, [], {}, detail(-1), detail('14'), detail(6), detail(8)], 14],
        ['maximum safe count', [detail(Number.MAX_SAFE_INTEGER), detail(0)], Number.MAX_SAFE_INTEGER],
        ['unsafe sum', [detail(Number.MAX_SAFE_INTEGER), detail(1), detail(0)], undefined],
      ];
      try {
        for (const [, candidatesTokensDetails] of cases) {
          c.tracer.startSpan('call_llm', {attributes: {
            'gcp.vertex.agent.llm_response': JSON.stringify({content: {parts: [{text: 'PRIVATE_OUTPUT'}]}, usageMetadata: {
              promptTokenCount: 100, cachedContentTokenCount: 40, candidatesTokenCount: 14, thoughtsTokenCount: 4,
              totalTokenCount: 118, promptTokensDetails: [{modality: 'AUDIO', tokenCount: 80}], candidatesTokensDetails,
            }}),
          }}).end();
        }
        c.tracer.startSpan('call_llm', {attributes: {
          'gcp.vertex.agent.llm_response': `{"usageMetadata":{"candidatesTokensDetails":[{"modality":"${modality}","tokenCount":1e400}]}}`,
        }}).end();
        // Overflow in one modality must not suppress a valid different modality.
        c.tracer.startSpan('call_llm', {attributes: {
          'gcp.vertex.agent.llm_response': JSON.stringify({usageMetadata: {candidatesTokensDetails: [
            {modality: other, tokenCount: Number.MAX_SAFE_INTEGER}, {modality: other, tokenCount: 1}, detail(14),
          ]}}),
        }}).end();
        await c.provider.forceFlush();
        for (let i = 0; i < cases.length; i++) {
          const a = c.spans[i].attributes;
          assert.equal(a[key], cases[i][2], cases[i][0]);
          assert.equal(a['gen_ai.usage.input_tokens.audio'], 80);
          assert.equal(a['gen_ai.usage.input_tokens'], 100);
          assert.equal(a['gen_ai.usage.cache_read_tokens'], 40);
          assert.equal(a['gen_ai.usage.output_tokens'], 18);
          assert.equal(a['gen_ai.usage.output_tokens.reasoning'], 4);
          assert.equal(a['gen_ai.usage.total_tokens'], 118);
          assert.equal(a['gcp.vertex.agent.llm_response'], undefined);
          assert.equal(a['output.value'] !== undefined, captureContent);
          if (!captureContent) assert(!JSON.stringify(a).includes('PRIVATE_OUTPUT'));
        }
        assert.equal(c.spans.at(-2).attributes[key], undefined);
        const detailOnly = c.spans.at(-1).attributes;
        assert.equal(detailOnly[key], 14);
        assert.equal(detailOnly[`gen_ai.usage.output_tokens.${other.toLowerCase()}`], undefined);
        for (const missing of ['input_tokens', 'input_tokens.audio', 'cache_read_tokens', 'output_tokens', 'output_tokens.reasoning', 'total_tokens']) {
          assert.equal(detailOnly[`gen_ai.usage.${missing}`], undefined, 'details alone must not fabricate totals');
        }
      } finally { await c.provider.shutdown(); }
    });
  }
}

test('reported cache reads are independent of content capture and never subtracted from prompt totals', async () => {
  for (const captureContent of [false, true]) {
    const c = collect({captureContent});
    try {
      for (const cachedContentTokenCount of [60, 0, -1, '60', null]) {
        c.tracer.startSpan('call_llm', {attributes: {
          'gcp.vertex.agent.llm_response': JSON.stringify({usageMetadata: {promptTokenCount: 100, cachedContentTokenCount}}),
        }}).end();
      }
      await c.provider.forceFlush();
      assert.deepEqual(c.spans.map(s => s.attributes['gen_ai.usage.cache_read_tokens']), [60, 0, undefined, undefined, undefined]);
      assert(c.spans.every(s => s.attributes['gen_ai.usage.input_tokens'] === 100));
      assert(c.spans.every(s => s.attributes['gen_ai.usage.total_tokens'] === undefined));
    } finally { await c.provider.shutdown(); }
  }
});
