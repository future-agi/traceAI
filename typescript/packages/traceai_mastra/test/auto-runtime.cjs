/* Native Node fixture: Jest's VM loader does not run node:module load hooks. */
const assert = require('node:assert/strict');
const path = require('node:path');
const [scenario, format = 'cjs'] = process.argv.slice(2);
const spans = [];
let flushes = 0;
let shutdowns = 0;
const stateKey = Symbol.for('traceai.auto.agent.v1');
const processor = {
  onStart() {},
  onEnd(span) {
    if (scenario === 'broken-processor') throw new Error('private-do-not-log');
    spans.push(span);
  },
  async forceFlush() { flushes++; },
  async shutdown() { shutdowns++; },
};
const modulePath = path.resolve(__dirname, '../dist/src/auto.js');
let enableMastra;

async function main() {
  ({ enableMastra } = process.env.TEST_MASTRA_ESM_ENTRY === '1'
    ? await import('@traceai/mastra/auto')
    : require(scenario === 'root-enable' ? '../dist/src/index.js' : modulePath));
  const baseline = scenario === 'baseline';
  if (scenario === 'missing-agent') {
    assert.equal(enableMastra(), false);
    assert.equal(enableMastra(), false);
    console.log(JSON.stringify({ ok: true }));
    return;
  }
  globalThis[stateKey] = {
    config: { apiKey: 'test-fi-key', secretKey: 'test-fi-secret', projectName: 'auto-mastra-test', projectType: 'observe', sampleRate: scenario === 'sampling-zero' ? 0 : 1 },
    processor,
  };
  let realAgent;
  if (scenario.startsWith('real-agent')) {
    // Exercise the actual cross-package sanitizer and wire dedup boundary, with
    // an in-memory collector and transport. No credentials or network required.
    delete globalThis[stateKey];
    globalThis.fetch = async () => new Response(JSON.stringify({
      id: 'mock', model: 'mock-model', choices: [{ message: { role: 'assistant', content: 'answer' } }],
    }), { headers: { 'content-type': 'application/json' } });
    realAgent = require('@traceai/auto');
    realAgent.init({
      apiKey: 'test-fi-key', secretKey: 'test-fi-secret', projectName: 'auto-mastra-test',
      instrumentations: ['mastra', 'wire'], wireCapture: true,
      traceConfig: { hideInputs: true, hideOutputs: true },
      exporter: {
        export(batch, callback) { spans.push(...batch); callback({ code: 0 }); },
        async shutdown() {}, async forceFlush() {},
      },
    });
    const report = realAgent.report();
    assert.equal(report.active, true);
    assert.equal(report.integrations.find(entry => entry.name === 'wire').status, 'instrumented');
    if (scenario === 'real-agent-bootstrap') {
      assert.equal(report.integrations.find(entry => entry.name === 'mastra').status, 'instrumented');
      assert(globalThis[Symbol.for('traceai.mastra.auto.v1')]?.enabled, 'auto bootstrap installed the hook without an application enable call');
    }
  }
  if (scenario === 'denylist' || scenario === 'allowlist') {
    if (scenario === 'denylist') globalThis[stateKey].config.disabledInstrumentations = ['mastra'];
    else globalThis[stateKey].config.instrumentations = ['wire'];
    assert.equal(enableMastra(), false);
    console.log(JSON.stringify({ ok: true }));
    return;
  }
  if (scenario === 'unsupported-node') {
    require('node:module').registerHooks = undefined;
    assert.equal(enableMastra(), false);
    console.log(JSON.stringify({ ok: true }));
    return;
  }
  if (scenario === 'disabled') {
    process.env.OTEL_SDK_DISABLED = 'true';
    assert.equal(enableMastra(), false);
    console.log(JSON.stringify({ ok: true }));
    return;
  }
  if (!baseline && scenario !== 'real-agent-bootstrap') {
    const fetchBefore = globalThis.fetch;
    assert.equal(enableMastra(), true);
    assert.equal(globalThis.fetch, fetchBefore, 'Mastra never installs its own transport/credential observer');
    const shared = globalThis[Symbol.for('traceai.mastra.auto.v1')];
    assert.equal(enableMastra(), true);
    assert.equal(globalThis[Symbol.for('traceai.mastra.auto.v1')], shared);
    delete require.cache[modulePath];
    assert.equal(require(modulePath).enableMastra(), true, 'second package copy shares hooks');
  }
  if (scenario === 'boundary-parity') {
    const shared = globalThis[Symbol.for('traceai.mastra.auto.v1')];
    const expected = new Error('original-error');
    const promise = Promise.resolve('original-result');
    let calls = 0;
    class Span {
      traceId = 'a'.repeat(32); id = 'b'.repeat(16); type = 'model_generation';
      executeInContext(fn) { calls++; return fn(); }
      executeInContextSync(fn) { calls++; return fn(); }
    }
    shared.wrapSpan(Span);
    shared.wrapSpan(Span);
    const span = new Span();
    assert.equal(span.executeInContext(() => promise), promise);
    assert.throws(() => span.executeInContextSync(() => { throw expected; }), error => error === expected);
    assert.equal(calls, 2, 'wrapped context invokes the application exactly once');
    let constructed = 0;
    class InvalidObservability { constructor() { constructed++; throw expected; } }
    const Wrapped = shared.wrapObservability(InvalidObservability);
    assert.throws(() => new Wrapped(), error => error === expected);
    assert.equal(constructed, 1);
    let getterCalls = 0;
    class App { constructor(config) { this.value = config.observability; } }
    const WrappedApp = shared.wrapMastra(App);
    const obj = new WrappedApp({ get observability() { getterCalls++; return 42; } });
    assert.equal(obj.value, 42);
    assert.equal(getterCalls, 1, 'configuration getter not called by instrumentation');
    console.log(JSON.stringify({ ok: true }));
    return;
  }
  const load = name => format === 'esm' ? import(name) : Promise.resolve(require(name));
  const { Mastra } = await load(scenario === 'subpath' ? '@mastra/core/mastra' : '@mastra/core');
  const { Agent } = await load('@mastra/core/agent');
  const { createMockModel } = await load('@mastra/core/test-utils/llm-mock');
  const { Observability, DefaultObservabilityInstance, BaseExporter } = await load('@mastra/observability');
  const { SpanType } = await load('@mastra/core/observability');
  const appEvents = [];
  class AppExporter extends BaseExporter {
    name = 'application-exporter';
    async _exportTracingEvent(event) { appEvents.push(event); }
    async shutdown() {}
    async flush() {}
  }
  const appExporter = new AppExporter();
  const configured = ['configured', 'pre-instantiated', 'manual-dedup', 'empty', 'failure-parity', 'late-instance', 'real-agent-pre', 'real-agent-bridge'].includes(scenario);
  let observability;
  const originalExporters = Object.freeze([appExporter]);
  if (scenario === 'real-agent-bridge') {
    const bridge = { name: 'application-bridge', createSpan() {}, executeInContext: (_id, fn) => fn(), executeInContextSync: (_id, fn) => fn(), async exportTracingEvent() {}, async flush() {}, async shutdown() {} };
    observability = new Observability({ configs: { app: { serviceName: 'bridge', exporters: [appExporter], bridge } } });
    assert.equal(observability.getDefaultInstance().getBridge(), bridge);
  } else if (scenario === 'late-instance') {
    observability = new Observability({ configs: {} });
    const instance = new DefaultObservabilityInstance({ name: 'late', serviceName: 'late-service', exporters: [appExporter] });
    observability.registerInstance('late', instance, true);
    observability.registerInstance('late-again', instance);
    assert.equal(instance.getExporters().filter(e => e.name === 'future-agi').length, 1);
  } else if (scenario === 'pre-instantiated' || scenario === 'real-agent-pre') {
    const instance = new DefaultObservabilityInstance({ name: 'pre', serviceName: 'pre-service', exporters: [appExporter] });
    observability = new Observability({ configs: { pre: instance }, sensitiveDataFilter: false });
    assert.equal(observability.getInstance('pre'), instance);
  } else if (scenario === 'manual-dedup') {
    appExporter.name = 'future-agi';
    observability = new Observability({ configs: { app: { serviceName: 'manual', exporters: [appExporter] } } });
  } else if (scenario === 'empty') {
    observability = new Observability({ configs: {}, default: { enabled: false } });
  } else if (configured) {
    observability = new Observability(Object.freeze({
      configs: Object.freeze({
        one: Object.freeze({ serviceName: 'app-one', exporters: originalExporters }),
        two: Object.freeze({ serviceName: 'app-two', exporters: Object.freeze([appExporter]) }),
      }),
      configSelector: () => 'one',
      sensitiveDataFilter: false,
    }));
    assert.equal(originalExporters.length, 1, 'application array was not appended to');
    assert.equal(observability.getInstance('one').getExporters()[0], appExporter);
  }
  const model = createMockModel({ mockText: 'The forecast is sunny.' });
  let transportCalls = 0;
  if (realAgent) {
    const { trace } = require('@opentelemetry/api');
    for (const method of ['doGenerate', 'doStream']) {
      const original = model[method].bind(model);
      model[method] = (...args) => trace.getTracer('mock-native-sdk').startActiveSpan('native-sdk', async native => {
        try {
          transportCalls++;
          const response = await fetch('https://api.openai.com/v1/chat/completions', {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ model: 'mock-model', messages: [{ role: 'user', content: 'test-fi-key private-wire-input' }] }),
          });
          await response.json();
          return original(...args);
        } finally { native.end(); }
      });
    }
  }
  const weather = new Agent({
    id: 'weather', name: 'weather', instructions: 'Answer briefly.', model,
  });
  const config = Object.freeze({ logger: false, agents: { weather }, ...(observability ? { observability } : {}) });
  class ApplicationMastra extends Mastra { ownMethod() { return 42; } }
  const mastra = new ApplicationMastra(config);
  assert(mastra instanceof Mastra);
  assert(mastra instanceof ApplicationMastra);
  assert.equal(mastra.ownMethod(), 42);
  assert.equal(config.observability, observability, 'original config object not mutated');
  if (observability) assert.equal(mastra.observability, observability, 'application observability not replaced');
  if (!baseline && scenario !== 'empty') {
    for (const instance of mastra.observability.listInstances().values()) {
      assert.equal(instance.getExporters().filter(e => e.name === 'future-agi').length, 1);
    }
  }
  if (scenario === 'failure-parity') {
    // The actual Mastra constructor's error class/message stays the same when
    // given invalid observability configuration. No catch-and-retry of the app.
    assert.throws(() => new Observability({ configs: { invalid: {} } }), /serviceName/);
  }
  const registered = mastra.getAgent('weather');
  const underParent = fn => {
    if (scenario !== 'real-agent-parent-off') return fn();
    const { context, trace } = require('@opentelemetry/api');
    return context.with(trace.setSpan(context.active(), trace.wrapSpanContext({
      traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 0,
    })), fn);
  };
  const generated = await underParent(() => registered.generate('What is the forecast?'));
  const streamed = await underParent(() => registered.stream('What is the forecast?'));
  const chunks = [];
  for await (const chunk of streamed.textStream) chunks.push(chunk);
  const streamText = chunks.join('');
  assert.equal(generated.text, 'The forecast is sunny.');
  assert.equal(streamText, generated.text);
  assert.equal(typeof streamed.getFullOutput, 'function', 'stream response helper remains present');

  // Drain Mastra's event delivery before its exporters; this also exercises the
  // normal graceful shutdown path rather than sleeping/polling for conversions.
  await mastra.observability.shutdown();
  assert.equal(shutdowns, 0, 'Mastra never owns the shared agent processor');
  if (scenario === 'empty' || scenario === 'manual-dedup' || scenario === 'sampling-zero' || scenario === 'real-agent-parent-off' || scenario === 'broken-processor' || baseline) {
    assert.equal(spans.length, 0);
  } else {
    assert(spans.some(s => s.attributes['gen_ai.span.kind'] === 'AGENT'));
    assert(spans.some(s => s.attributes['gen_ai.span.kind'] === 'LLM'));
    const ids = spans.map(s => s.spanContext().spanId);
    assert.equal(new Set(ids).size, ids.length, 'each ended span forwarded exactly once');
    for (const span of spans) {
      assert.equal(span.resource.attributes.project_name, 'auto-mastra-test');
      assert.equal(span.resource.attributes.project_type, 'observe');
      assert(span.endTime[0] >= span.startTime[0]);
      assert.equal(span.ended, true);
    }
    if (!realAgent) assert(flushes > 0);
  }
  if (realAgent) {
    assert(transportCalls >= 2, 'generate and stream both exercised the transport');
    await globalThis[stateKey].processor.forceFlush();
    assert.equal(spans.filter(span => span.attributes['traceai.capture.layer'] === 'wire').length, 0, 'Mastra model context suppresses duplicate wire spans');
    const serialized = JSON.stringify(spans.map(span => ({ attributes: span.attributes, events: span.events, status: span.status })));
    for (const hidden of ['test-fi-key', 'test-fi-secret', 'What is the forecast?', 'The forecast is sunny.']) {
      assert.equal(serialized.includes(hidden), false, `agent privacy boundary hides ${hidden}`);
    }
    await realAgent.shutdown();
  }
  if (scenario === 'configured' || scenario === 'pre-instantiated') {
    assert(appEvents.some(event => event.type === 'span_ended' && event.exportedSpan.type === SpanType.AGENT_RUN));
    assert.equal(originalExporters.length, 1);
  }
  console.log(JSON.stringify({ ok: true, text: generated.text, streamText, spanCount: spans.length }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
