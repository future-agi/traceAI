/* Sequential native-loader regression fixture; run under the suite's heavy.sh slot. */
const assert = require('node:assert/strict');
const { context, trace, isSpanContextValid } = require('@opentelemetry/api');
const [scenario, format = 'cjs'] = process.argv.slice(2);
const load = name => format === 'esm' ? import(name) : Promise.resolve(require(name));
const key = Symbol.for('traceai.auto.agent.v1');
const spans = [];
const parents = [];
const requests = [];
const baseline = scenario.endsWith('-baseline');
const toolScenario = scenario.startsWith('tool');
const never = scenario.startsWith('never');
const bridgeScenario = scenario.startsWith('bridge');
const cachedConfig = scenario === 'cached-config';
const sampleRate = scenario === 'zero-native' ? 0 : ['sampled-parent', 'detached-parent', 'cached-config'].includes(scenario) ? 0.01 : 1;
const upstream = { traceId: 'ffffffff000000000000000000000000', spanId: 'b'.repeat(16), traceFlags: 1 };

async function main() {
  // Reproduce a static root import BEFORE the real agent initializes/installs hooks.
  const entry = scenario === 'root-enable' || cachedConfig ? await load('@traceai/mastra') : undefined;
  // Hold the exact original constructor before enable; the proxy cannot rewrite
  // this application import binding, especially in native ESM.
  const cachedObservability = cachedConfig ? (await load('@mastra/observability')).Observability : undefined;
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: options.body });
    return new Response(JSON.stringify({ id: 'mock', model: 'mock-model', choices: [{ message: { role: 'assistant', content: 'answer' } }] }),
      { headers: { 'content-type': 'application/json' } });
  };
  const auto = require('@traceai/auto');
  auto.init({
    apiKey: 'test-fi-key', secretKey: 'test-fi-secret', projectName: 'mastra-regression', sampleRate,
    instrumentations: baseline ? ['wire'] : ['mastra', 'wire'], wireCapture: true,
    exporter: { export(batch, callback) { spans.push(...batch); callback({ code: 0 }); }, async shutdown() {}, async forceFlush() {} },
  });
  const report = auto.report();
  assert.equal(report.active, true);
  assert.equal(report.integrations.find(item => item.name === 'wire').status, 'instrumented', 'wire must actually be enabled');
  if (entry) assert.equal(entry.enableMastra(), true);
  const { Mastra } = await load('@mastra/core');
  const { Agent } = await load('@mastra/core/agent');
  const { createMockModel } = await load('@mastra/core/test-utils/llm-mock');
  const { Observability, BaseExporter, SamplingStrategyType, getExternalParentId } = await load('@mastra/observability');
  const appEvents = [];
  class AppExporter extends BaseExporter {
    name = 'app';
    async _exportTracingEvent(event) { appEvents.push(event); }
    async flush() {}
    async shutdown() {}
  }
  const appExporter = new AppExporter();
  let bridge;
  const bridgeSpans = new Map();
  if (bridgeScenario) {
    const native = bridgeSpans;
    const tracer = trace.getTracer('application-bridge');
    bridge = {
      name: 'application-native-bridge',
      createSpan(options) {
        let parent = context.active();
        const parentId = getExternalParentId(options);
        if (options.parent && parentId) parent = trace.setSpanContext(parent, {
          traceId: options.parent.traceId, spanId: parentId, traceFlags: trace.getSpanContext(parent)?.traceFlags ?? 1,
        });
        const kind = { agent_run: 'AGENT', model_generation: 'LLM', model_inference: 'LLM', tool_call: 'TOOL' }[options.type] ?? 'CHAIN';
        const span = tracer.startSpan(options.name, { attributes: { 'gen_ai.span.kind': kind, 'test.bridge': true } }, parent);
        const identity = span.spanContext();
        native.set(identity.spanId, span);
        return { traceId: identity.traceId, spanId: identity.spanId, parentSpanId: trace.getSpanContext(parent)?.spanId };
      },
      executeInContext(id, fn) { return context.with(trace.setSpan(context.active(), native.get(id)), fn); },
      executeInContextSync(id, fn) { return context.with(trace.setSpan(context.active(), native.get(id)), fn); },
      async exportTracingEvent(event) {
        if (event.type === 'span_ended') {
          native.get(event.exportedSpan.id)?.end();
          native.delete(event.exportedSpan.id);
        }
      },
      async flush() {}, async shutdown() {},
    };
  }
  let observability;
  if (never || bridgeScenario || scenario === 'sampled-parent' || scenario === 'detached-parent' || cachedConfig) {
    const Constructor = cachedObservability ?? Observability;
    const originalExporters = cachedConfig ? Object.freeze([appExporter]) : [appExporter];
    const config = { serviceName: 'app', exporters: originalExporters, ...(bridge ? { bridge } : {}),
      ...(scenario.startsWith('bridge-copy') ? { spanOutputProcessors: [{
        name: 'application-copy',
        process(span) {
          const copy = Object.create(Object.getPrototypeOf(span), Object.getOwnPropertyDescriptors(span));
          if (scenario.startsWith('bridge-copy-custom')) {
            // A supported processor can also supply its own export method. The
            // association must happen at the original instance's export boundary.
            copy.exportSpan = (...args) => ({ ...span.exportSpan(...args) });
          }
          return copy;
        }, async shutdown() {},
      }] } : {}),
      ...(never ? { sampling: { type: SamplingStrategyType.NEVER } } : {}) };
    observability = new Constructor({ configs: { app: cachedConfig ? Object.freeze(config) : config } });
    if (cachedConfig) assert.equal(originalExporters.length, 1, 'cached configuration array is unchanged');
    if (bridge) assert.equal(observability.getDefaultInstance().getBridge(), bridge, 'preserve the real application bridge');
    if (cachedConfig) {
      const instance = observability.getDefaultInstance();
      assert(instance.getBridge(), 'cached constructor plain configs receive a context bridge');
      assert.equal(instance.getBridge(), instance.getBridge(), 'fallback bridge identity is stable');
    }
  }
  const tracer = trace.getTracer('mock-native-sdk');
  async function transport() {
    const parent = trace.getSpanContext(context.active());
    parents.push(parent);
    assert(parent && isSpanContextValid(parent), 'model/tool parent stays valid');
    if (bridgeScenario) {
      const selected = trace.getSpan(context.active());
      assert.equal(selected, bridgeSpans.get(parent.spanId), 'preserve the exact application SDK span object');
      assert.equal(selected.isRecording(), true, 'application bridge span remains recording');
      selected.setAttribute('app.marker', 'kept');
      selected.addEvent('application-event');
    }
    return tracer.startActiveSpan('native-sdk', async native => {
      try {
        const response = await fetch('https://api.openai.com/v1/chat/completions', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ model: 'mock-model', messages: [{ role: 'user', content: 'hello' }] }),
        });
        await response.json();
      } finally { native.end(); }
    });
  }
  const model = createMockModel({ mockText: 'Finished.' });
  let tools;
  let workflow;
  if (toolScenario) {
    const { createTool } = await load('@mastra/core/tools');
    const { createWorkflow, createStep } = await load('@mastra/core/workflows');
    const { z } = require('zod');
    let calls = 0;
    const toolResult = () => ({
      rawCall: { rawPrompt: null, rawSettings: {} }, warnings: [], finishReason: 'tool-calls',
      usage: { inputTokens: 1, outputTokens: 1 },
      content: [{ type: 'tool-call', toolCallId: 'call-1', toolName: 'lookup', input: '{}' }],
    });
    const generate = model.doGenerate.bind(model);
    const stream = model.doStream.bind(model);
    model.doGenerate = async (...args) => calls++ === 0 ? toolResult() : generate(...args);
    model.doStream = async (...args) => {
      if (calls++ !== 0) return stream(...args);
      return { warnings: [], stream: new ReadableStream({ start(controller) {
        controller.enqueue({ type: 'stream-start', warnings: [] });
        controller.enqueue(toolResult().content[0]);
        controller.enqueue({ type: 'finish', finishReason: 'tool-calls', usage: { inputTokens: 1, outputTokens: 1 } });
        controller.close();
      } }) };
    };
    tools = { lookup: createTool({ id: 'lookup', description: 'Look up an answer', inputSchema: z.object({}),
      execute: async () => { await transport(); return { text: 'answer' }; },
    }) };
    const step = createStep({ id: 'agent-step', inputSchema: z.object({}), outputSchema: z.object({ text: z.string() }),
      execute: async ({ mastra }) => ({ text: (await mastra.getAgent('worker').generate('Use lookup then answer.')).text }),
    });
    workflow = createWorkflow({ id: 'tool-workflow', inputSchema: z.object({}), outputSchema: z.object({ text: z.string() }) }).then(step).commit();
  } else {
    for (const method of ['doGenerate', 'doStream']) {
      const original = model[method].bind(model);
      model[method] = async (...args) => { await transport(); return original(...args); };
    }
  }
  const worker = new Agent({ id: 'worker', name: 'worker', instructions: 'Answer briefly.', model, ...(tools ? { tools } : {}) });
  const mastra = new Mastra({ logger: false, agents: { worker }, ...(workflow ? { workflows: { workflow } } : {}), ...(observability ? { observability } : {}) });
  if (cachedConfig) {
    assert.equal(mastra.observability, observability);
    const exporters = observability.getDefaultInstance().getExporters();
    assert.equal(exporters[0], appExporter, 'application exporter identity is preserved');
    assert.equal(exporters.filter(item => item.name === 'future-agi').length, 1, 'cached configuration registers one agent exporter');
  }
  let output;
  const underParent = fn => context.with(trace.setSpanContext(context.active(), upstream), fn);
  if (toolScenario) {
    const run = await mastra.getWorkflow('workflow').createRun();
    const result = await underParent(() => run.start({ inputData: {} }));
    assert.equal(result.status, 'success');
    output = result.result.text;
    assert.equal(requests.length, 1, 'exactly one uncovered tool transport call');
  } else {
    const invoke = () => mastra.getAgent('worker').generate('Finish.');
    output = (await (never || ['sampled-parent', 'detached-parent', 'cached-config'].includes(scenario) ? underParent(invoke) : invoke())).text;
  }
  assert.equal(output, 'Finished.');

  if (scenario === 'detached-parent') {
    // End outside the sampled upstream context at a low ratio. Delivery must
    // use the creation decision rather than re-rooting/re-sampling.
    const { SpanType } = await load('@mastra/core/observability');
    const instance = mastra.observability.getDefaultInstance();
    const detached = underParent(() => instance.startSpan({ name: 'detached', type: SpanType.AGENT_RUN, attributes: {} }));
    detached.end();
  }
  await mastra.observability.shutdown();
  await globalThis[key].processor.forceFlush();
  const wire = spans.filter(span => span.attributes['traceai.capture.layer'] === 'wire');
  const framework = spans.filter(span => span.instrumentationScope.name === '@traceai/mastra' || span.attributes['test.bridge']);
  const ids = spans.map(span => span.spanContext().spanId);
  assert.equal(new Set(ids).size, ids.length, 'native bridge and converter must not export duplicate identities');
  if (never) {
    assert.equal(framework.length, 0);
    assert.equal(wire.length, 1, 'Mastra sampling never must leave wire fallback available');
    assert.equal(parents[0].traceId, upstream.traceId);
    assert.equal(parents[0].spanId, upstream.spanId, 'NoOpSpan must not replace valid upstream context');
  } else if (toolScenario) {
    assert.equal(wire.length, 1, 'TOOL execution must not inherit LLM coverage');
    const wireParent = wire[0].parentSpanContext ?? { spanId: wire[0].parentSpanId };
    assert(spans.some(span => span.name === 'native-sdk' && span.spanContext().spanId === wireParent.spanId));
    if (!baseline) assert(framework.some(span => span.attributes['gen_ai.span.kind'] === 'TOOL'));
  } else if (scenario === 'zero-native') {
    assert.equal(parents[0].traceFlags, 0, 'root carrier honors zero head sampling');
    assert.equal(spans.length, 0, 'no sampled native descendants with missing framework parents');
  } else {
    assert.equal(wire.length, baseline && bridgeScenario ? 1 : 0, 'library model coverage survives a bridge and native SDK span');
    assert(framework.some(span => span.attributes['gen_ai.span.kind'] === 'LLM'));
    const identities = new Set(ids);
    for (const span of spans) {
      const parent = span.parentSpanContext?.spanId ?? span.parentSpanId;
      if (parent) assert(identities.has(parent) || parent === upstream.spanId, `missing parent of ${span.name}: ${parent}`);
    }
    assert.equal(new Set(spans.map(span => span.spanContext().traceId)).size, 1, 'connected trace');
    if (scenario === 'sampled-parent' || cachedConfig) {
      assert(framework.length > 0, 'sampled upstream keeps all Mastra ancestors despite a low ratio');
      assert(spans.every(span => span.spanContext().traceId === upstream.traceId));
    }
    if (scenario === 'detached-parent') assert(spans.some(span => span.name.includes('detached')));
  }
  if (bridgeScenario) {
    assert(appEvents.some(event => event.type === 'span_ended'), 'preserved application exporter receives events');
    assert(spans.some(span => span.attributes['app.marker'] === 'kept' && span.events.some(event => event.name === 'application-event')), 'application SDK updates reach export');
    const nativeIds = spans.filter(span => span.attributes['test.bridge']).map(span => span.spanContext().spanId);
    assert(nativeIds.length > 0);
    for (const id of nativeIds) assert.equal(spans.filter(span => span.spanContext().spanId === id).length, 1);
  }
  await auto.shutdown();
  console.log(JSON.stringify({ ok: true, output, requests, wireCount: wire.length, total: spans.length, framework: framework.length, parent: parents[0] }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
