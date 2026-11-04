const { test, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createRequire } = require('node:module');
const fixtureJson = process.env.FI_CORE_PACKAGE_JSON || path.resolve(__dirname, '../package.json');
const load = createRequire(fixtureJson);
for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
const { context, trace, ROOT_CONTEXT, createContextKey } = load('@opentelemetry/api');
const { suppressTracing, isTracingSuppressed } = load('@opentelemetry/core');
const { NodeTracerProvider } = load('@opentelemetry/sdk-trace-node');
const { InMemorySpanExporter, SimpleSpanProcessor } = load('@opentelemetry/sdk-trace-base');
const cjs = load('@traceai/fi-core');
const providers = [];
function memory() {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  provider.register(); providers.push(provider);
  return { provider, exporter };
}
const invocation = () => ({ awsRequestId: 'synthetic-request', functionName: 'synthetic-function',
  getRemainingTimeInMillis: () => 10000, callbackWaitsForEmptyEventLoop: false });
async function esmEntry() {
  const packageJson = load(fixtureJson);
  return import(pathToFileURL(path.resolve(path.dirname(fixtureJson), packageJson.exports['.'].import)).href);
}
function disabledProvider(mode) {
  if (mode === 'disabled') return undefined;
  return { getTracer() {
    if (mode === 'initialization failure') throw new Error('synthetic initialization failure');
    return { startSpan() { throw new Error('synthetic span failure'); } };
  }, forceFlush: async () => {} };
}
function wrap(original, provider, completion) {
  const handler = cjs.traceLambdaHandler(completion === 'promise' ? original :
    (_event, _invocation, callback) => { original().then(result => callback(null, result), callback); },
  { provider, completion });
  if (completion === 'promise') {
    assert.equal(handler.constructor.name, 'AsyncFunction', 'AWS must still classify a disabled handler as async');
    return () => handler({}, invocation());
  }
  return () => new Promise((resolve, reject) => handler({}, invocation(),
    (error, result) => error ? reject(error) : resolve(result)));
}
afterEach(async () => {
  for (const provider of providers.splice(0)) await provider.shutdown();
  trace.disable(); context.disable();
});

for (const mode of ['disabled', 'initialization failure', 'span failure']) {
  for (const completion of ['promise', 'callback']) {
    test(`${mode} ${completion} wrapper preserves application context and child parentage`, async () => {
      const { provider, exporter } = memory();
      const marker = createContextKey('synthetic-application-key');
      const parent = provider.getTracer('application').startSpan('application-parent');
      const original = async () => {
        await Promise.resolve();
        provider.getTracer('application').startSpan('application-child').end();
        return context.active().getValue(marker);
      };
      const wrapped = wrap(original, disabledProvider(mode), completion);
      const appContext = trace.setSpan(ROOT_CONTEXT.setValue(marker, 'synthetic-value'), parent);
      await context.with(appContext, async () => {
        assert.equal(await original(), 'synthetic-value');
        assert.equal(await wrapped(), 'synthetic-value');
        assert.equal(trace.getActiveSpan(), parent);
      });
      parent.end();
      const children = exporter.getFinishedSpans().filter(span => span.name === 'application-child');
      assert.equal(children.length, 2);
      for (const child of children) {
        assert.equal(child.parentSpanContext?.spanId ?? child.parentSpanId, parent.spanContext().spanId);
        assert.equal(child.spanContext().traceId, parent.spanContext().traceId);
      }
      assert.equal(exporter.getFinishedSpans().length, 3);
    });

    test(`${mode} ${completion} wrapper preserves tracing suppression after await`, async () => {
      const { provider, exporter } = memory();
      const original = async () => {
        await Promise.resolve();
        provider.getTracer('application').startSpan('sensitive-operation').end();
        return isTracingSuppressed(context.active());
      };
      const wrapped = wrap(original, disabledProvider(mode), completion);
      await context.with(suppressTracing(ROOT_CONTEXT), async () => {
        assert.equal(await original(), true);
        assert.equal(await wrapped(), true);
      });
      assert.equal(exporter.getFinishedSpans().length, 0);
    });
  }
}

for (const direction of ['CJS to ESM', 'ESM to CJS']) {
  test(`double wrapping is idempotent across public exports: ${direction}`, async () => {
    const { provider, exporter } = memory();
    const esm = await esmEntry();
    const [innerModule, outerModule] = direction === 'CJS to ESM' ? [cjs, esm] : [esm, cjs];
    const inner = innerModule.traceLambdaHandler(async () => 42, { provider });
    const outer = outerModule.traceLambdaHandler(inner, { provider });
    assert.equal(await outer({}, invocation()), 42);
    assert.equal(exporter.getFinishedSpans().filter(span => span.name === 'aws.lambda.invoke').length, 1);
    assert.equal(outer, inner);
  });

  test(`public exports share pending flushes and release settled state: ${direction}`, async () => {
    const { provider } = memory();
    const esm = await esmEntry();
    const [firstModule, secondModule] = direction === 'CJS to ESM' ? [cjs, esm] : [esm, cjs];
    let flushCalls = 0, settle;
    provider.forceFlush = () => {
      flushCalls++;
      return new Promise(resolve => { settle = resolve; });
    };
    const options = { provider, maxFlushWaitMillis: 5 };
    const first = firstModule.traceLambdaHandler(async () => 42, options);
    const second = secondModule.traceLambdaHandler(async () => 42, options);
    try {
      assert.equal(await first({}, invocation()), 42);
      for (let i = 0; i < 3; i++) assert.equal(await second({}, invocation()), 42);
      assert.equal(flushCalls, 1, 'one pending flush per provider across package exports');
      settle();
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(await second({}, invocation()), 42);
      assert.equal(flushCalls, 2, 'a settled flush must not block subsequent invocations');
      assert.equal(await first({}, invocation()), 42);
      assert.equal(flushCalls, 2);
    } finally { settle?.(); }
  });
}
