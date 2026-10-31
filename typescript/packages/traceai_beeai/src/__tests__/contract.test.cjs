// Real BeeAI, synthetic work only. Run with BEEAI_MODULE_ROOT pointing at an
// isolated npm install; BEEAI_MODE=esm exercises the framework's ESM copy.
const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const { createServer } = require('node:http');
const { once } = require('node:events');
const { BeeAIInstrumentation, isPatched } = require('../../dist/src');
const core = createRequire(require.resolve('@traceai/fi-core'));
const { context, diag, trace, SpanStatusCode } = require('@opentelemetry/api');
const { NodeTracerProvider } = core('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = core('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = core('@opentelemetry/exporter-trace-otlp-http');
const { SemanticConventions: SC } = require('@traceai/fi-semantic-conventions');
const sdk = createRequire(resolve(process.env.BEEAI_MODULE_ROOT, 'package.json'));
const rootOnly = process.env.BEEAI_REGISTRATION === 'root';
const automatic = process.env.BEEAI_REGISTRATION === 'auto';
const wire = [];
const memory = new InMemorySpanExporter();
let provider, instrumentation, server, RunContext, BaseAgent, Emitter, Workflow, DynamicTool, StringToolOutput, z, ProbeAgent, ChatModel, ChatModelOutput, AssistantMessage, UserMessage, OfflineModel;
async function upstream(name) {
  if (process.env.BEEAI_MODE !== 'esm') return sdk(name);
  return import(pathToFileURL(sdk.resolve(name).replace(/\.cjs$/, '.js')).href);
}

before(async () => {
  server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    wire.push(JSON.parse(Buffer.concat(chunks).toString()));
    response.writeHead(200, { 'content-type': 'application/json' }).end('{}');
  }).listen(0, '127.0.0.1');
  await once(server, 'listening');
  provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(memory),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${server.address().port}/v1/traces` })),
  ] });
  provider.register();
  if (automatic) instrumentation = new BeeAIInstrumentation();
  const root = await upstream('beeai-framework');
  ({ BaseAgent } = root);
  ({ RunContext } = await upstream('beeai-framework/context'));
  ({ Emitter } = await upstream('beeai-framework/emitter/emitter'));
  ({ Workflow } = await upstream('beeai-framework/workflows/workflow'));
  ({ DynamicTool, StringToolOutput } = await upstream('beeai-framework/tools/base'));
  ({ z } = await upstream('zod'));
  ({ ChatModel, ChatModelOutput } = await upstream('beeai-framework/backend/chat'));
  ({ AssistantMessage, UserMessage } = await upstream('beeai-framework/backend/message'));
  OfflineModel = class OfflineModel extends ChatModel {
    emitter = Emitter.root.child({ namespace: ['backend', 'offline', 'chat'], creator: this });
    modelId = 'offline-model';
    providerId = 'offline';
    async _create() { return new ChatModelOutput([new AssistantMessage('synthetic-output')], { promptTokens: 2, completionTokens: 3, totalTokens: 5 }); }
    async *_createStream() { yield await this._create(); yield await this._create(); }
  };
  if (!automatic) {
    instrumentation = new BeeAIInstrumentation();
    instrumentation.manuallyInstrument(rootOnly ? root : { RunContext });
  }
  instrumentation.setTracerProvider(provider);
  ProbeAgent = class ProbeAgent extends BaseAgent {
    emitter = Emitter.root.child({ namespace: ['agent', 'probe'], creator: this });
    constructor(handler = async () => ({ text: 'synthetic-output' })) { super(); this.handler = handler; }
    async _run(input, options, ctx) { return this.handler(input, options, ctx); }
  };
});
beforeEach(async () => { await provider.forceFlush(); memory.reset(); wire.length = 0; });
after(async () => {
  instrumentation?.disable();
  await provider?.shutdown();
  await new Promise(resolve => server.close(resolve));
  context.disable();
  trace.disable();
});

function spans() { return memory.getFinishedSpans(); }
function parentId(span) { return span.parentSpanContext?.spanId ?? span.parentSpanId; }
function kind(span) { return span.attributes[SC.FI_SPAN_KIND]; }
function nativeRun(namespace, handler, input = { text: 'synthetic-input' }) {
  const instance = { emitter: Emitter.root.child({ namespace }), modelId: 'offline-model', providerId: 'offline' };
  return RunContext.enter(instance, { params: [input] }, handler);
}

test('real root/BaseAgent registration retains lazy identity and all Run chaining', async () => {
  let calls = 0, observations = 0, middlewares = 0;
  const agent = new ProbeAgent(async () => { calls++; return { text: 'synthetic-output' }; });
  const run = agent.run({ prompt: 'synthetic-input' });
  assert.equal(typeof run.observe, 'function');
  assert.equal(run.observe(() => observations++), run);
  assert.equal(run.context({ marker: 'application-context' }), run);
  assert.equal(run.middleware(ctx => { middlewares++; assert.equal(ctx.context.marker, 'application-context'); }), run);
  assert.equal(calls, 0);
  assert.equal(spans().length, 0);
  await run;
  assert.equal(calls, 1);
  assert.equal(observations, 1);
  assert.equal(middlewares, 1);
  assert.equal(spans().length, 1);
  assert.equal(kind(spans()[0]), 'AGENT');
  assert.equal(spans()[0].status.code, SpanStatusCode.OK);
});

test('nested real workflow, tool and model lifecycle spans have exact parents and no duplicates', async () => {
  const tool = new DynamicTool({
    name: 'offline_tool', description: 'Synthetic tool', inputSchema: z.object({ text: z.string() }),
    handler: async input => new StringToolOutput(input.text),
  });
  const workflow = new Workflow({ name: 'offline', schema: z.object({ text: z.string() }) })
    .addStep('call', async state => {
      await tool.run(state);
      await new OfflineModel().create({ messages: [new UserMessage(state.text)] });
      return Workflow.END;
    });
  const agent = new ProbeAgent(async input => { await workflow.run({ text: input.prompt }); return 'done'; });
  const parent = provider.getTracer('test').startSpan('application');
  await context.with(trace.setSpan(context.active(), parent), () => agent.run({ prompt: 'synthetic-input' }));
  parent.end();
  assert.equal(spans().length, 5);
  const agentSpan = spans().find(s => kind(s) === 'AGENT');
  const workflowSpan = spans().find(s => kind(s) === 'CHAIN');
  const toolSpan = spans().find(s => kind(s) === 'TOOL');
  const modelSpan = spans().find(s => kind(s) === 'LLM');
  assert.equal(parentId(agentSpan), parent.spanContext().spanId);
  assert.equal(parentId(workflowSpan), agentSpan.spanContext().spanId);
  for (const child of [toolSpan, modelSpan]) {
    assert.equal(parentId(child), workflowSpan.spanContext().spanId);
    assert.equal(child.spanContext().traceId, parent.spanContext().traceId);
  }
  assert.equal(modelSpan.attributes[SC.LLM_MODEL_NAME], 'offline-model');
  assert.equal(modelSpan.attributes[SC.LLM_TOKEN_COUNT_TOTAL], 5);
  await provider.forceFlush();
  const exported = wire.flatMap(r => r.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(exported.length, 5);
  assert.equal(new Set(exported.map(s => s.traceId)).size, 1);
  assert.equal(exported.find(s => s.spanId === modelSpan.spanContext().spanId).parentSpanId, workflowSpan.spanContext().spanId);
});

test('real ChatModel streaming keeps observers and emits one aggregate model span', async () => {
  let tokens = 0;
  await new ProbeAgent(async () => new OfflineModel().create({ messages: [new UserMessage('stream')], stream: true })
    .observe(emitter => emitter.on('newToken', () => tokens++))).run({ prompt: 'stream' });
  assert.equal(tokens, 2);
  assert.equal(spans().length, 2);
  assert.equal(spans().filter(s => kind(s) === 'LLM').length, 1);
  assert.equal(spans().find(s => kind(s) === 'LLM').attributes[SC.LLM_TOKEN_COUNT_TOTAL], 5);
});

test('standalone model and workflow runs are traced without an agent', { skip: rootOnly }, async () => {
  await new OfflineModel().create({ messages: [new UserMessage('standalone')] });
  await new Workflow({ schema: z.object({ text: z.string() }) })
    .addStep('end', async () => Workflow.END).run({ text: 'standalone' });
  assert.deepEqual(spans().map(kind).sort(), ['CHAIN', 'LLM']);
});

test('concurrent roots never cross-parent and unused Runs create no spans', async () => {
  new ProbeAgent().run({ prompt: 'never-started' });
  await Promise.all([1, 2, 3].map(() => new ProbeAgent(async () => {
    await nativeRun(['tool', 'offline'], async () => { await new Promise(r => setImmediate(r)); return 'done'; });
    return 'done';
  }).run({ prompt: 'concurrent' })));
  assert.equal(spans().length, 6);
  const agents = spans().filter(s => kind(s) === 'AGENT');
  assert.equal(new Set(agents.map(s => s.spanContext().traceId)).size, 3);
  for (const agent of agents) assert.equal(spans().filter(s => parentId(s) === agent.spanContext().spanId).length, 1);
});

test('propagates execution context into external OTel spans', { skip: rootOnly }, async () => {
  await new ProbeAgent(async () => {
    await Promise.resolve();
    const child = provider.getTracer('external-sdk').startSpan('external'); child.end();
    return 'done';
  }).run({ prompt: 'context' });
  const agent = spans().find(s => kind(s) === 'AGENT');
  assert.equal(parentId(spans().find(s => s.name === 'external')), agent.spanContext().spanId);
});

test('errors and cancellation end spans exactly once and preserve rejection', async () => {
  await assert.rejects(new ProbeAgent(async () => { throw new Error('synthetic-failure'); }).run({ prompt: 'failure' }));
  assert.equal(spans().length, 1);
  assert.equal(spans()[0].status.code, SpanStatusCode.ERROR);
  const controller = new AbortController();
  let entered;
  const ready = new Promise(resolve => { entered = resolve; });
  const run = new ProbeAgent(async () => { entered(); await new Promise(() => {}); })
    .run({ prompt: 'abort' }, { signal: controller.signal });
  const result = Promise.resolve(run);
  await ready;
  controller.abort(new Error('synthetic-cancel'));
  await assert.rejects(result);
  assert.equal(spans().length, 2);
  assert.ok(spans().every(s => s.status.code === SpanStatusCode.ERROR));
});

test('circular payloads are omitted with a diagnostic, not an application failure', async () => {
  const circular = {}; circular.self = circular;
  const warnings = [];
  const warn = diag.warn;
  diag.warn = (...args) => warnings.push(args);
  try {
    assert.equal(await new ProbeAgent(async () => circular).run(circular), circular);
    assert.equal(spans().length, 1);
    assert.equal(spans()[0].status.code, SpanStatusCode.OK);
    assert.equal(warnings.length, 2);
    assert.equal(spans()[0].attributes[SC.INPUT_VALUE], undefined);
  } finally { diag.warn = warn; }
});

test('a failing tracer cannot change the application result', async () => {
  const warnings = [];
  const warn = instrumentation._diag.warn;
  instrumentation._diag.warn = (...args) => warnings.push(args);
  instrumentation.setTracerProvider({ getTracer: () => ({ startSpan() { throw new Error('synthetic telemetry failure'); } }) });
  try {
    assert.deepEqual(await new ProbeAgent().run({ prompt: 'success' }), { text: 'synthetic-output' });
    assert.equal(spans().length, 0);
    assert.ok(warnings.length > 0);
  } finally { instrumentation.setTracerProvider(provider); instrumentation._diag.warn = warn; }
});

test('manual registration is idempotent, handles frozen namespaces, and disable/re-enable restores methods', async () => {
  const original = RunContext.enter;
  instrumentation.manuallyInstrument(Object.freeze({ RunContext }));
  instrumentation.manuallyInstrument(Object.freeze({ RunContext }));
  const wrapped = RunContext.enter;
  const duplicate = new BeeAIInstrumentation();
  duplicate.manuallyInstrument({ RunContext });
  assert.equal(RunContext.enter, wrapped);
  duplicate.disable();
  assert.equal(RunContext.enter, wrapped);
  await new ProbeAgent().run({ prompt: 'once' });
  assert.equal(spans().length, 1);
  instrumentation.disable();
  assert.equal(isPatched(), false);
  await new ProbeAgent().run({ prompt: 'disabled' });
  assert.equal(spans().length, 1);
  instrumentation.enable();
  await new ProbeAgent().run({ prompt: 'enabled' });
  assert.equal(spans().length, 2);
  assert.notEqual(RunContext.enter, original);
});

for (const traceConfig of [
  { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
  { hideInputMessages: true, hideOutputMessages: true },
]) test(`privacy flags reach the OTLP wire: ${Object.keys(traceConfig).join(', ')}`, async () => {
  instrumentation.disable();
  const privateInstrumentation = new BeeAIInstrumentation({ traceConfig });
  privateInstrumentation.setTracerProvider(provider);
  privateInstrumentation.manuallyInstrument({ RunContext });
  try {
    const model = new OfflineModel();
    model._create = async () => new ChatModelOutput([new AssistantMessage('WIRE_SECRET_OUTPUT')]);
    await new ProbeAgent(async () => model.create({ messages: [new UserMessage('WIRE_SECRET_INPUT')] }))
      .run({ prompt: 'WIRE_SECRET_INPUT' });
    await assert.rejects(new ProbeAgent(async () => { throw new Error('WIRE_SECRET_ERROR'); }).run({ prompt: 'WIRE_SECRET_INPUT' }));
    await provider.forceFlush();
    assert.equal(spans().length, 3);
    assert.ok(!JSON.stringify(wire).includes('WIRE_SECRET'));
    for (const span of spans()) {
      assert.equal(span.attributes[SC.RAW_INPUT], undefined);
      assert.equal(span.attributes[SC.RAW_OUTPUT], undefined);
    }
    assert.equal(spans()[2].status.code, SpanStatusCode.ERROR);
  } finally { privateInstrumentation.disable(); instrumentation.enable(); }
});
