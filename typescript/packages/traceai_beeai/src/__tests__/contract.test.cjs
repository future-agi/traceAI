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

// Regression contracts use real framework results and assert the exported wire,
// not just the direct attributes that FITracer already knows how to mask.
for (const flag of ['FI_HIDE_INPUTS', 'FI_HIDE_INPUT_MESSAGES', 'FI_HIDE_INPUT_TEXT']) {
  test(`agent memory never leaks a hidden prompt: ${flag}`, async () => {
    const previous = process.env[flag];
    process.env[flag] = 'true';
    try {
      const agentPath = sdk('beeai-framework/package.json').version === '0.1.2' ? 'bee' : 'react';
      const exports = await upstream(`beeai-framework/agents/${agentPath}/agent`);
      const RealAgent = exports.ReActAgent ?? exports.BeeAgent;
      const { UnconstrainedMemory } = await upstream('beeai-framework/memory/unconstrainedMemory');
      const model = new OfflineModel();
      model._create = async () => new ChatModelOutput([new AssistantMessage('Thought: done\nFinal Answer: public-answer')]);
      model._createStream = async function* () { yield await this._create(); };
      const result = await new RealAgent({ llm: model, tools: [], memory: new UnconstrainedMemory() })
        .run({ prompt: 'PRIVATE_AGENT_PROMPT' });
      assert.ok(result.memory.messages.some(m => m.text.includes('PRIVATE_AGENT_PROMPT')));
      assert.equal(result.result.text, 'public-answer');
      // Applications can return an agent result inside a tool/workflow state.
      await new ProbeAgent(() => nativeRun(['tool', 'nested_result'], async () => ({ nested: result })))
        .run({ prompt: 'public-input' });
      await provider.forceFlush();
      assert.ok(!JSON.stringify(wire).includes('PRIVATE_AGENT_PROMPT'));
      const output = JSON.parse(spans().find(s => kind(s) === 'AGENT').attributes[SC.OUTPUT_VALUE]);
      assert.deepEqual(Object.keys(output), ['result']);
      assert.ok(JSON.stringify(output).includes('public-answer'));
    } finally {
      if (previous === undefined) delete process.env[flag]; else process.env[flag] = previous;
    }
  });
}

for (const configSource of ['environment', 'options']) {
  test(`embedding hiding covers enclosing payloads without mutation: ${configSource}`, async () => {
    const previous = process.env.FI_HIDE_EMBEDDING_VECTORS;
    process.env.FI_HIDE_EMBEDDING_VECTORS = configSource === 'environment' ? 'true' : 'false';
    let privateInstrumentation;
    if (configSource === 'options') {
      instrumentation.disable();
      privateInstrumentation = new BeeAIInstrumentation({ traceConfig: { hideEmbeddingVectors: true } });
      privateInstrumentation.setTracerProvider(provider);
      privateInstrumentation.manuallyInstrument(rootOnly ? { BaseAgent } : { RunContext });
    }
    try {
      const { EmbeddingModel } = await upstream('beeai-framework/backend/embedding');
      class OfflineEmbedding extends EmbeddingModel {
        emitter = Emitter.root.child({ namespace: ['backend', 'offline', 'embedding'], creator: this });
        async _create() { return { embeddings: [[0.123456789, 0.987654321]], values: ['public-text'] }; }
      }
      const tool = new DynamicTool({
        name: 'vector_consumer', description: 'Synthetic vector consumer',
        inputSchema: z.object({ embeddings: z.array(z.array(z.number())) }),
        handler: async input => ({ nested: { vector: input.embeddings[0] }, label: 'public-tool' }),
      });
      const workflow = new Workflow({ schema: z.object({ text: z.string(), embeddings: z.array(z.array(z.number())).optional(), tool: z.any().optional() }) })
        .addStep('embed', async state => {
          state.embeddings = (await new OfflineEmbedding().create({ values: [state.text] })).embeddings;
          state.tool = await tool.run({ embeddings: state.embeddings });
          return Workflow.END;
        });
      const result = await new ProbeAgent(() => workflow.run({ text: 'public-text' })).run({ prompt: 'embed' });
      assert.deepEqual(result.state.embeddings, [[0.123456789, 0.987654321]]);
      assert.deepEqual(result.state.tool.nested.vector, [0.123456789, 0.987654321]);
      await provider.forceFlush();
      assert.deepEqual(spans().map(kind).sort(), ['AGENT', 'CHAIN', 'EMBEDDING', 'TOOL']);
      assert.ok(!JSON.stringify(wire).includes('0.123456789'));
      assert.ok(!JSON.stringify(wire).includes('0.987654321'));
      for (const span of spans().filter(s => kind(s) !== 'EMBEDDING')) {
        assert.ok(span.attributes[SC.OUTPUT_VALUE].includes('public-'));
        assert.ok(span.attributes[SC.OUTPUT_VALUE].includes('__REDACTED__'));
      }
    } finally {
      if (previous === undefined) delete process.env.FI_HIDE_EMBEDDING_VECTORS;
      else process.env.FI_HIDE_EMBEDDING_VECTORS = previous;
      if (privateInstrumentation) { privateInstrumentation.disable(); instrumentation.enable(); }
    }
  });
}

test('structured model objects and supplied nested usage are captured', async () => {
  const model = new OfflineModel();
  model._create = async () => new ChatModelOutput([new AssistantMessage('{"answer":"structured-answer"}')], { promptTokens: 2, completionTokens: 3, totalTokens: 5 });
  const result = await new ProbeAgent(() => model.createStructure({
    messages: [new UserMessage('structured-input')], schema: z.object({ answer: z.string() }),
  })).run({ prompt: 'structured-input' });
  assert.deepEqual(result.object, { answer: 'structured-answer' });
  const span = spans().find(s => kind(s) === 'LLM');
  assert.deepEqual(JSON.parse(span.attributes[SC.OUTPUT_VALUE]), result.object);
  assert.equal(span.attributes[SC.OUTPUT_MIME_TYPE], 'application/json');
  if (result.output) {
    assert.ok(span.attributes[SC.LLM_OUTPUT_MESSAGES].includes('structured-answer'));
    assert.equal(span.attributes[SC.LLM_TOKEN_COUNT_PROMPT], 2);
    assert.equal(span.attributes[SC.LLM_TOKEN_COUNT_COMPLETION], 3);
    assert.equal(span.attributes[SC.LLM_TOKEN_COUNT_TOTAL], 5);
  } else {
    assert.equal(span.attributes[SC.LLM_TOKEN_COUNT_TOTAL], undefined, 'do not invent missing usage');
  }
});

for (const flag of ['FI_HIDE_OUTPUTS', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_OUTPUT_TEXT']) {
  test(`structured model content respects ${flag}`, async () => {
    const previous = process.env[flag];
    process.env[flag] = 'true';
    try {
      const model = new OfflineModel();
      model._create = async () => new ChatModelOutput([new AssistantMessage('{"answer":"PRIVATE_STRUCTURED_ANSWER"}')], { promptTokens: 2, completionTokens: 3, totalTokens: 5 });
      const result = await new ProbeAgent(() => model.createStructure({
        messages: [new UserMessage('public-input')], schema: z.object({ answer: z.string() }),
      })).run({ prompt: 'public-input' });
      assert.equal(result.object.answer, 'PRIVATE_STRUCTURED_ANSWER');
      await provider.forceFlush();
      assert.ok(!JSON.stringify(wire).includes('PRIVATE_STRUCTURED_ANSWER'));
      if (result.output) assert.equal(spans().find(s => kind(s) === 'LLM').attributes[SC.LLM_TOKEN_COUNT_TOTAL], 5);
    } finally {
      if (previous === undefined) delete process.env[flag]; else process.env[flag] = previous;
    }
  });
}

test('nested errors and success serialization are observed once per lifecycle event', async () => {
  let failures = 0;
  await assert.rejects(new ProbeAgent(() => nativeRun(['tool', 'offline'], async () => {
    failures++; throw new Error('single-failure');
  })).run({ prompt: 'fail' }));
  assert.equal(failures, 1);
  assert.equal(spans().length, 2);
  for (const span of spans()) assert.equal(span.events.filter(e => e.name === 'exception').length, 1);
  let serializations = 0;
  await new ProbeAgent(async () => {
    await nativeRun(['tool', 'offline'], async () => ({ toJSON() { serializations++; return { text: 'public-output' }; } }));
    return 'done';
  }).run({ prompt: 'success' });
  assert.equal(serializations, 1);
});

test('configured tool names distinguish otherwise identical tool calls', async () => {
  const names = ['lookup_order', 'cancel_order'];
  await new ProbeAgent(async () => {
    for (const name of names) await new DynamicTool({
      name, description: 'Synthetic order tool', inputSchema: z.object({ id: z.string() }),
      handler: async () => new StringToolOutput('ok'),
    }).run({ id: 'synthetic-order' });
    return 'done';
  }).run({ prompt: 'tools' });
  const tools = spans().filter(s => kind(s) === 'TOOL');
  assert.deepEqual(tools.map(s => s.attributes[SC.TOOL_NAME]), names);
  assert.deepEqual(tools.map(s => s.name), names.map(name => `${name} Run`));
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
