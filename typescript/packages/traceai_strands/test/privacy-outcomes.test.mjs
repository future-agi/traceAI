import assert from 'node:assert/strict';
import { after, beforeEach, afterEach, test } from 'node:test';
import { require, sdk, agent, FixtureModel, SECRET } from './fixture.mjs';
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { StrandsSpanExporter } = require('@traceai/strands/native');
const memory = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
provider.register();
const flags = ['FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_ERROR_DETAILS', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_OUTPUT_TEXT', 'FI_HIDE_INPUT_IMAGES', 'FI_HIDE_EMBEDDING_VECTORS', 'FI_BASE64_IMAGE_MAX_LENGTH', 'FI_PII_REDACTION', 'OTEL_SEMCONV_STABILITY_OPT_IN'];
const saved = new Map(flags.map(f => [f, process.env[f]]));
beforeEach(() => { memory.reset(); for (const f of flags) delete process.env[f]; });
afterEach(() => { for (const f of flags) { if (saved.get(f) === undefined) delete process.env[f]; else process.env[f] = saved.get(f); } });
after(() => provider.shutdown());
function project(options = {}) {
  let result;
  new StrandsSpanExporter({ export(spans, cb) { result = spans; cb({code: 0}); }, async shutdown() {} }, options).export(memory.getFinishedSpans(), () => {});
  return result;
}
const operation = span => span.attributes['gen_ai.operation.name'];
const snapshot = span => ({name:span.name, status:span.status, attributes:span.attributes, events:span.events, links:span.links});

test('cancelled native invocation is not exported as successful', async () => {
  const a = agent('cancelled');
  let result;
  for await (const event of a.stream('safe input')) {
    if (event.type === 'modelStreamUpdateEvent') a.cancel();
    if (event.type === 'agentResultEvent') result = event.result;
  }
  await provider.forceFlush();
  assert.equal(result.stopReason, 'cancelled');
  const root = project().find(s => operation(s) === 'invoke_agent');
  assert.notEqual(root.status.code, 1, 'SDK returned stopReason=cancelled but adapter exported root status=OK');
});

test('early iterator return is not exported as successful completion', async () => {
  let consumedModelEvent = false;
  for await (const event of agent('early').stream('safe input')) {
    if (event.type === 'modelStreamUpdateEvent') { consumedModelEvent = true; break; }
  }
  assert.ok(consumedModelEvent);
  await provider.forceFlush();
  const list = project();
  assert.equal(list.length, 3);
  assert.ok(list.every(s => s.status.code !== 1), 'incomplete model, cycle and root were all exported OK');
});

for (const [flag, attr] of [['FI_HIDE_INPUTS', 'input.value'], ['FI_HIDE_OUTPUTS', 'output.value']]) {
  test(`${flag} hides native mapped content even when captureContent is enabled`, async () => {
    process.env[flag] = 'true';
    await agent('privacy').invoke(SECRET);
    await provider.forceFlush();
    const models = project({captureContent:true}).filter(s => operation(s) === 'chat');
    assert.ok(models.length > 0);
    assert.ok(models.every(s => !String(s.attributes[attr]).includes(SECRET)), `${flag}=true ignored in ${attr}`);
  });
}
for (const [flag, attr] of [['FI_HIDE_INPUT_MESSAGES', 'gen_ai.input.messages'], ['FI_HIDE_OUTPUT_MESSAGES', 'gen_ai.output.messages']]) {
  test(`${flag} hides canonical native messages`, async () => {
    process.env[flag] = 'true';
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = 'gen_ai_latest_experimental,gen_ai_span_attributes_only';
    await agent('privacy').invoke(SECRET);
    await provider.forceFlush();
    const models = project({captureContent:true}).filter(s => operation(s) === 'chat');
    assert.ok(models.length > 0);
    assert.ok(models.every(s => !String(s.attributes[attr]).includes(SECRET)), `${flag}=true ignored in ${attr}`);
  });
}
test('FI_HIDE_ERROR_DETAILS hides native exceptions even when error capture is enabled', async () => {
  process.env.FI_HIDE_ERROR_DETAILS = 'true';
  await assert.rejects(agent('failure', 'error').invoke('safe input'));
  await provider.forceFlush();
  const list = project({captureErrorDetails:true});
  assert.ok(list.some(s => s.status.code === 2));
  assert.ok(!JSON.stringify(list.map(snapshot)).includes(SECRET), 'FI_HIDE_ERROR_DETAILS=true ignored in status/events');
});

for (const conventions of ['', 'gen_ai_latest_experimental', 'gen_ai_latest_experimental,gen_ai_span_attributes_only']) {
  test(`tool errors require error-detail opt-in (${conventions || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = conventions;
    const a = new sdk.Agent({ name:'tool-error', printer:false, model:new FixtureModel('tool'), tools:[new sdk.FunctionTool({name:'echo', description:'Fails', callback() { throw new Error('synthetic-tool-error-only'); }})] });
    await a.invoke('safe input');
    await provider.forceFlush();
    const tool = project({captureContent:true, captureErrorDetails:false}).find(s => operation(s) === 'execute_tool');
    assert.equal(tool.status.code, 2);
    assert.ok(!JSON.stringify(project({captureContent:true}).map(snapshot)).includes('synthetic-tool-error-only'), 'error survives in tool output or subsequent model/cycle history');
    assert.ok(JSON.stringify(project({captureContent:true, captureErrorDetails:true}).map(snapshot)).includes('synthetic-tool-error-only'), 'explicit error opt-in keeps details');
  });
}

test('tool result status=error must not be exported with span status OK', async () => {
  const a = new sdk.Agent({name:'missing-tool', printer:false, model:new FixtureModel('tool'), tools:[]});
  await a.invoke('safe input');
  await provider.forceFlush();
  const tool = project().find(s => operation(s) === 'execute_tool');
  assert.ok(tool, 'SDK should trace the unavailable tool attempt');
  assert.equal(tool.attributes['gen_ai.tool.status'], 'error');
  assert.equal(tool.status.code, 2, 'tool error result was exported OK because SDK did not throw an Error object');
});

const conventions = ['', 'gen_ai_latest_experimental', 'gen_ai_latest_experimental,gen_ai_span_attributes_only'];
const INPUT = 'synthetic-private-input';
const OUTPUT = 'synthetic-private-output';
class DistinctModel extends FixtureModel {
  async *stream(...args) {
    for await (const event of super.stream(...args)) {
      if (event.delta?.type === 'textDelta') event.delta.text = OUTPUT;
      yield event;
    }
  }
}

for (const convention of conventions) {
  for (const flag of ['FI_HIDE_INPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_INPUT_IMAGES',
    'FI_HIDE_OUTPUTS', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_OUTPUT_TEXT']) {
    test(`${flag} covers native payloads and projections (${convention || 'default'})`, async () => {
      process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
      process.env[flag] = 'true';
      const a = new sdk.Agent({name:'directions', printer:false, model:new DistinctModel(), systemPrompt:INPUT,
        traceAttributes: {unknownPayload: flag.includes('INPUT') ? INPUT : OUTPUT}});
      await a.invoke(INPUT);
      const before = JSON.stringify(memory.getFinishedSpans().map(snapshot));
      const list = project({captureContent:true, captureErrorDetails:true});
      const payload = JSON.stringify(list.map(snapshot));
      assert.ok(!payload.includes(flag.includes('INPUT') ? INPUT : OUTPUT), 'hidden content escaped via native payload or derived copy');
      assert.ok(payload.includes(flag.includes('INPUT') ? OUTPUT : INPUT), 'unhidden direction remains useful');
      assert.equal(JSON.stringify(memory.getFinishedSpans().map(snapshot)), before, 'raw spans are unchanged');
      assert.ok(list.some(s => s.attributes['gen_ai.response.finish_reasons']?.includes('endTurn')), 'completion survives content hiding');
    });
  }

  for (const mode of ['completed', 'cancelled', 'early', 'error']) {
    test(`outcome is preserved with metadata only: ${mode} (${convention || 'default'})`, async () => {
      process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
      const a = agent('lifecycle', mode === 'error' ? 'error' : 'text');
      if (mode === 'error') await assert.rejects(a.invoke('safe'));
      else for await (const event of a.stream('safe')) {
        if (event.type === 'modelStreamUpdateEvent') {
          if (mode === 'early') break;
          if (mode === 'cancelled') a.cancel();
        }
      }
      const expected = mode === 'completed' ? 1 : mode === 'early' ? 0 : 2;
      const raw = memory.getFinishedSpans();
      const before = JSON.stringify(raw.map(snapshot));
      // Cover a full batch, reversed batch, and separate processor batches.
      for (const batches of [[raw], [[...raw].reverse()], raw.map(s => [s])]) {
        const collected = [];
        const adapter = new StrandsSpanExporter({export(spans, cb) { collected.push(...spans); cb({code:0}); }, async shutdown() {}});
        for (const batch of batches) adapter.export(batch, () => {});
        assert.equal(collected.length, 3);
        assert.ok(collected.every(s => s.status.code === expected), JSON.stringify(collected.map(snapshot)));
        const root = collected.find(s => operation(s) === 'invoke_agent');
        if (mode === 'cancelled') assert.deepEqual(root.attributes['gen_ai.response.finish_reasons'], ['cancelled']);
        if (mode === 'early') assert.equal(root.attributes['strands.operation.outcome'], 'unknown');
        assert.ok(!JSON.stringify(collected.map(snapshot)).includes(SECRET));
        assert.deepEqual(collected.map(s => s.spanContext()).sort((a,b) => a.spanId.localeCompare(b.spanId)),
          raw.map(s => s.spanContext()).sort((a,b) => a.spanId.localeCompare(b.spanId)));
      }
      assert.equal(JSON.stringify(raw.map(snapshot)), before);
    });
  }
}

test('environment error hiding overrides opt-ins and derived hiding protects tool history', async () => {
  await new sdk.Agent({name:'failure', printer:false, model:new FixtureModel('tool'), tools:[new sdk.FunctionTool({name:'echo', description:'Fails', callback() { throw new Error('synthetic-error-history'); }})]}).invoke('safe');
  for (const flag of ['FI_HIDE_ERROR_DETAILS', 'FI_HIDE_INPUTS', 'FI_HIDE_OUTPUTS']) {
    process.env[flag] = 'true';
    assert.ok(!JSON.stringify(project({captureContent:true, captureErrorDetails:true}).map(snapshot)).includes('synthetic-error-history'));
    delete process.env[flag];
  }
});

test('shared masking covers events, links, embeddings and status without mutating the raw span', () => {
  process.env.FI_PII_REDACTION = 'true';
  process.env.FI_HIDE_EMBEDDING_VECTORS = 'true';
  const email = 'synthetic@example.invalid';
  const span = provider.getTracer('strands-agents').startSpan('privacy', {attributes:{
    'custom.email':email, 'embedding.embeddings.0.embedding.vector':[1,2,3], 'gen_ai.operation.name':'execute_tool',
    'gen_ai.tool.status':'success', 'gen_ai.tool.call.result':email,
  }});
  span.addEvent('custom', {'payload':email});
  span.addLink({context:span.spanContext(), attributes:{payload:email, 'input.value':INPUT, 'output.value':OUTPUT}});
  span.setStatus({code:2, message:email});
  span.end();
  const before = JSON.stringify(memory.getFinishedSpans().map(snapshot));
  const list = project({captureContent:true, captureErrorDetails:true});
  assert.ok(!JSON.stringify(list.map(snapshot)).includes(email));
  assert.equal(list[0].attributes['embedding.embeddings.0.embedding.vector'], undefined);
  for (const [flag, hidden] of [['FI_HIDE_INPUTS',INPUT], ['FI_HIDE_OUTPUTS',OUTPUT]]) {
    process.env[flag] = 'true';
    assert.ok(!JSON.stringify(project({captureContent:true, captureErrorDetails:true}).map(snapshot)).includes(hidden));
    delete process.env[flag];
  }
  assert.equal(JSON.stringify(memory.getFinishedSpans().map(snapshot)), before);
});

test('image size limits and malformed native JSON fail closed', () => {
  process.env.FI_BASE64_IMAGE_MAX_LENGTH = '16';
  const image = `data:image/png;base64,${'A'.repeat(80)}`;
  const span = provider.getTracer('strands-agents').startSpan('chat', {attributes:{
    'gen_ai.operation.name':'chat', 'gen_ai.input.messages':JSON.stringify([{role:'user', content:[{type:'image_url', image_url:{url:image}}]}]),
    'gen_ai.output.messages':'[{"text":"synthetic-truncated-error',
  }});
  span.end();
  const payload = JSON.stringify(project({captureContent:true}).map(snapshot));
  assert.ok(!payload.includes(image));
  assert.ok(!payload.includes('synthetic-truncated-error'));
});

test('unknown finish metadata is not treated as evidence of success or exported as content', () => {
  const span = provider.getTracer('strands-agents').startSpan('chat', {attributes:{'gen_ai.operation.name':'chat'}});
  span.addEvent('gen_ai.choice', {finish_reason:SECRET, message:SECRET});
  span.setStatus({code:1}); span.end();
  const [result] = project();
  assert.equal(result.status.code, 0);
  assert.ok(!JSON.stringify(snapshot(result)).includes(SECRET));
});

test('explicit FI_HIDE_ERROR_DETAILS=false overrides derivation, not content hiding', async () => {
  process.env.FI_HIDE_INPUTS = 'true';
  process.env.FI_HIDE_ERROR_DETAILS = 'false';
  await assert.rejects(agent('failure', 'error').invoke(INPUT));
  const list = project({captureContent:true, captureErrorDetails:true});
  assert.ok(list.some(s => s.status.message?.includes(SECRET)));
  assert.ok(list.some(s => JSON.stringify(s.events).includes(SECRET)));
  assert.ok(!JSON.stringify(list.map(snapshot)).includes(INPUT));
  assert.ok(!JSON.stringify(project({captureContent:true, captureErrorDetails:false}).map(snapshot)).includes(SECRET));
});

for (const convention of conventions) {
  test(`successful tools keep their own content and recovered failures do not fail the agent (${convention || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
    await agent('success', 'tool').invoke(INPUT);
    const success = project({captureContent:true});
    const tool = success.find(s => operation(s) === 'execute_tool');
    assert.equal(tool.status.code, 1);
    assert.ok(tool.attributes['output.value'].includes(SECRET));
    assert.ok(success.every(s => s.status.code === 1));
    memory.reset();
    await new sdk.Agent({name:'recovered', printer:false, model:new FixtureModel('tool'), tools:[]}).invoke(INPUT);
    const recovered = project();
    assert.equal(recovered.find(s => operation(s) === 'execute_tool').status.code, 2);
    assert.equal(recovered.find(s => operation(s) === 'invoke_agent').status.code, 1);
  });
}

for (const convention of conventions) {
  test(`model output limit remains visible without content (${convention || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
    class LimitedModel extends FixtureModel {
      async *stream(...args) {
        for await (const event of super.stream(...args)) {
          if (event.type === 'modelMessageStopEvent') event.stopReason = 'maxTokens';
          yield event;
        }
      }
    }
    await assert.rejects(new sdk.Agent({name:'limited', printer:false, model:new LimitedModel()}).invoke(INPUT), {name:'MaxTokensError'});
    const list = project();
    assert.ok(list.every(s => s.status.code === 2));
    assert.deepEqual(list.find(s => operation(s) === 'invoke_agent').attributes['gen_ai.response.finish_reasons'], ['maxTokens']);
    assert.ok(!JSON.stringify(list.map(snapshot)).includes(SECRET));
  });
}

test('parallel success and cancellation keep independent cycle outcomes', async () => {
  await Promise.all([
    agent('completed').invoke(INPUT),
    (async () => {
      const a = agent('cancelled');
      for await (const event of a.stream(INPUT)) if (event.type === 'modelStreamUpdateEvent') a.cancel();
    })(),
  ]);
  const first = project();
  const second = project();
  const roots = first.filter(s => operation(s) === 'invoke_agent');
  assert.equal(roots.length, 2);
  for (const root of roots) {
    const expected = root.attributes['gen_ai.agent.name'] === 'completed' ? 1 : 2;
    assert.ok(first.filter(s => s.spanContext().traceId === root.spanContext().traceId).every(s => s.status.code === expected));
  }
  assert.deepEqual(first.map(snapshot), second.map(snapshot));
});
