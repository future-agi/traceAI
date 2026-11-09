import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';
import { require, sdk, FixtureModel } from './fixture.mjs';
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { StrandsSpanExporter, StrandsSpanProcessor } = require('@traceai/strands/native');
const memory = new InMemorySpanExporter();
const processor = new StrandsSpanProcessor(new SimpleSpanProcessor(memory));
const raw = new InMemorySpanExporter();
const provider = new NodeTracerProvider({spanProcessors:[processor, new SimpleSpanProcessor(raw)]});
provider.register();
after(() => provider.shutdown());
beforeEach(() => { memory.reset(); raw.reset(); delete process.env.OTEL_SEMCONV_STABILITY_OPT_IN; });
const operation = s => s.attributes['gen_ai.operation.name'];
function project(raw, order) {
  const result = [];
  const exporter = new StrandsSpanExporter({export(spans, cb) {result.push(...spans); cb({code:0});}, async shutdown() {}});
  const batches = order === 'split' ? raw.map(s => [s]) : [order === 'reversed' ? [...raw].reverse() : raw];
  for (const batch of batches) exporter.export(batch, () => {});
  return result;
}
function checkRoot(kind, code, raw = memory.getFinishedSpans()) {
  for (const order of ['full', 'reversed', 'split']) {
    const roots = project(raw, order).filter(s => operation(s) === kind);
    assert.equal(roots.length, 1, 'exactly one native root, not a replacement span');
    assert.equal(roots[0].status.code, code, order);
    if (code === 0) assert.equal(roots[0].attributes['strands.operation.outcome'], 'unknown');
  }
}
function graph() {
  const models = [new FixtureModel(), new FixtureModel()];
  return {models, runner:new sdk.Graph({nodes:['a','b'].map((id,i) => new sdk.Agent({id,printer:false,model:models[i]})),edges:[['a','b']],maxSteps:4})};
}
class HandoffModel extends FixtureModel {
  constructor(target) { super('swarm'); this.target = target; }
  async *stream(...args) {
    for await (const event of super.stream(...args)) {
      if (event.delta?.type === 'textDelta') event.delta.text = JSON.stringify({message:'synthetic handoff',agentId:this.target});
      yield event;
    }
  }
}
function swarm(fail = false) {
  return new sdk.Swarm({nodes:(fail ? ['a','b','c'] : ['safe']).map(id => new sdk.Agent({id,printer:false,
    model: fail ? new HandoffModel(id === 'a' ? 'b' : 'a') : new FixtureModel('swarm')})),
    maxSteps:10, ...(fail ? {repetitiveHandoffDetectionWindow:3,repetitiveHandoffMinUniqueAgents:3} : {})});
}
for (const convention of ['', 'gen_ai_latest_experimental', 'gen_ai_latest_experimental,gen_ai_span_attributes_only']) {
  test(`explicit FAILED swarm result overrides completed nodes (${convention || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
    const result = await processor.invoke(() => swarm(true).invoke('safe'));
    assert.equal(result.status, 'FAILED');
    assert.match(result.error.message, /Repetitive handoff/);
    assert.ok(memory.getFinishedSpans().filter(s => operation(s) === 'execute_node').every(s => s.attributes['gen_ai.agent.status'] === 'COMPLETED'));
    checkRoot('invoke_swarm', 2);
    const nativeRoot = raw.getFinishedSpans().find(s => operation(s) === 'invoke_swarm');
    assert.equal(nativeRoot.status.code, 1, 'the SDK span and other processors are not mutated');
    assert.equal(nativeRoot.attributes['strands.orchestration.status'], undefined);
    assert.deepEqual(memory.getFinishedSpans().map(s => s.spanContext()).sort((a,b) => a.spanId.localeCompare(b.spanId)),
      raw.getFinishedSpans().map(s => s.spanContext()).sort((a,b) => a.spanId.localeCompare(b.spanId)), 'all native IDs are preserved once');
    assert.ok(!JSON.stringify(memory.getFinishedSpans().map(s => s.attributes)).includes(result.error.message), 'result error payload is never copied');
  });
  test(`early graph handoff remains unknown (${convention || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
    const {runner,models} = graph();
    let early = false;
    for await (const event of processor.stream(() => runner.stream('safe'))) {
      if (event.type === 'multiAgentHandoffEvent') { early = true; break; }
      assert.notEqual(event.type, 'multiAgentResultEvent');
    }
    assert.equal(early, true);
    assert.equal(models[1].calls, 0);
    checkRoot('invoke_graph', 0);
  });
  test(`complete invoke and stream controls stay successful (${convention || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
    for (const [runner,kind] of [[graph().runner,'invoke_graph'],[swarm(),'invoke_swarm']]) {
      memory.reset();
      assert.equal((await processor.invoke(() => runner.invoke('safe'))).status, 'COMPLETED');
      checkRoot(kind, 1);
      memory.reset();
      let result;
      for await (const event of processor.stream(() => runner.stream('safe'))) {
        if (event.type === 'multiAgentResultEvent') result = event.result;
      }
      assert.equal(result.status, 'COMPLETED');
      checkRoot(kind, 1);
    }
  });
  test(`exporter alone never infers success from children (${convention || 'default'})`, async () => {
    process.env.OTEL_SEMCONV_STABILITY_OPT_IN = convention;
    await swarm(true).invoke('safe');
    checkRoot('invoke_swarm', 0);
    memory.reset();
    await graph().runner.invoke('safe');
    checkRoot('invoke_graph', 0);
  });
}

test('concurrent orchestration results cannot cross-contaminate identical IDs', async () => {
  const [good, bad] = await Promise.all([processor.invoke(() => swarm().invoke('safe')), processor.invoke(() => swarm(true).invoke('safe'))]);
  assert.equal(good.status, 'COMPLETED'); assert.equal(bad.status, 'FAILED');
  for (const order of ['full','reversed','split']) {
    assert.deepEqual(project(memory.getFinishedSpans(), order).filter(s => operation(s) === 'invoke_swarm').map(s => s.status.code).sort(), [1,2]);
  }
});

test('streaming explicit failure and thrown errors remain errors', async () => {
  for await (const event of processor.stream(() => swarm(true).stream('safe'))) { /* consume public result */ }
  checkRoot('invoke_swarm', 2);
  memory.reset();
  const failing = new sdk.Graph({nodes:[new sdk.Agent({id:'fail',printer:false,model:new FixtureModel('error')})],edges:[],maxSteps:1});
  assert.equal((await processor.invoke(() => failing.invoke('safe'))).status, 'FAILED');
  checkRoot('invoke_graph', 2);
  memory.reset();
  const limited = new sdk.Graph({nodes:['a','b'].map(id => new sdk.Agent({id,printer:false,model:new FixtureModel()})),edges:[['a','b']],maxSteps:1});
  await assert.rejects(processor.invoke(() => limited.invoke('safe')), /max.*steps|step.*limit|maximum/i);
  checkRoot('invoke_graph', 2);
});

test('breaking on the public result preserves that explicit completion', async () => {
  for await (const event of processor.stream(() => graph().runner.stream('safe'))) {
    if (event.type === 'multiAgentResultEvent') break;
  }
  checkRoot('invoke_graph', 1);
});

test('flushing inside the after hook forwards an unknown root exactly once', async () => {
  const runner = graph().runner;
  const {AfterMultiAgentInvocationEvent} = require('@strands-agents/sdk/multiagent');
  runner.addHook(AfterMultiAgentInvocationEvent, async () => {
    assert.equal(memory.getFinishedSpans().filter(s => operation(s) === 'invoke_graph').length, 0);
    await provider.forceFlush();
    checkRoot('invoke_graph', 0);
  });
  assert.equal((await processor.invoke(() => runner.invoke('safe'))).status, 'COMPLETED');
  checkRoot('invoke_graph', 0);
});

test('an after-hook error is preserved even though the native root already ended OK', async () => {
  const runner = graph().runner;
  const {AfterMultiAgentInvocationEvent} = require('@strands-agents/sdk/multiagent');
  const expected = new Error('synthetic-after-hook-error');
  runner.addHook(AfterMultiAgentInvocationEvent, () => { throw expected; });
  await assert.rejects(processor.invoke(() => runner.invoke('safe')), error => error === expected);
  checkRoot('invoke_graph', 2);
  assert.equal(raw.getFinishedSpans().find(s => operation(s) === 'invoke_graph').status.code, 1);
});

test('unrelated scopes and nested orchestrations do not borrow outer completion', async () => {
  const tracer = provider.getTracer('application');
  await processor.invoke(async () => {
    const unrelated = tracer.startSpan('unrelated', {attributes:{'gen_ai.operation.name':'invoke_graph'}});
    unrelated.end();
    return graph().runner.invoke('safe');
  });
  const unrelated = memory.getFinishedSpans().find(s => s.name === 'unrelated');
  assert.equal(unrelated, raw.getFinishedSpans().find(s => s.name === 'unrelated'));
  memory.reset(); raw.reset();
  const inner = graph().runner;
  const outer = new sdk.Graph({id:'outer',nodes:[inner],edges:[],maxSteps:2});
  assert.equal((await processor.invoke(() => outer.invoke('safe'))).status, 'COMPLETED');
  const roots = project(memory.getFinishedSpans(), 'full').filter(s => operation(s) === 'invoke_graph');
  assert.equal(roots.length, 2);
  assert.equal(roots.find(s => s.attributes['gen_ai.agent.id'] === 'outer').status.code, 1);
  assert.equal(roots.find(s => s.attributes['gen_ai.agent.id'] !== 'outer').status.code, 0);
});

for (const mode of ['overflow', 'shutdown']) {
  test(`pending-root ${mode} forwards unknown once without altering the application result`, async () => {
    const forwarded = [];
    const local = new StrandsSpanProcessor({onStart() {}, onEnd(span) {forwarded.push(span);}, async forceFlush() {}, async shutdown() {}});
    const localProvider = new NodeTracerProvider({spanProcessors:[local]});
    const tracer = localProvider.getTracer('strands-agents');
    let release;
    const wait = new Promise(resolve => { release = resolve; });
    const count = mode === 'overflow' ? 4097 : 1;
    const calls = Array.from({length:count}, () => local.invoke(async () => {
      const span = tracer.startSpan('native-root', {attributes:{'gen_ai.operation.name':'invoke_graph'}});
      span.setStatus({code:1}); span.end();
      await wait;
      return {status:'COMPLETED'};
    }));
    if (mode === 'shutdown') await localProvider.shutdown();
    assert.equal(forwarded.length, 1);
    assert.equal(forwarded[0].status.code, 0);
    release();
    assert.ok((await Promise.all(calls)).every(result => result.status === 'COMPLETED'));
    assert.equal(forwarded.length, count);
    assert.equal(new Set(forwarded.map(s => s.spanContext().spanId)).size, count);
    assert.ok(forwarded.slice(1).every(s => s.status.code === 1));
    if (mode !== 'shutdown') await localProvider.shutdown();
  });
}
