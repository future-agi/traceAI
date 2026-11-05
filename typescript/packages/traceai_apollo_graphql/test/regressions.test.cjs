const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { FiSanitizingExporter } = require('@traceai/apollo-graphql');
const { ExportResultCode } = require('@opentelemetry/core');
const { diag } = require('@opentelemetry/api');

const env = { ...process.env, FI_API_KEY: 'synthetic', FI_SECRET_KEY: 'synthetic', FI_BASE_URL: 'http://127.0.0.1:9' };
function child(code, additions = {}) {
  return spawnSync(process.execPath, ['-e', code], {
    cwd: require('node:path').resolve(__dirname, '..'),
    env: { ...env, ...additions }, encoding: 'utf8', timeout: 5000,
  });
}

test('ambient OTLP collector tokens cannot be forwarded with explicit Future AGI credentials', () => {
  for (const variable of ['OTEL_EXPORTER_OTLP_HEADERS', 'OTEL_EXPORTER_OTLP_TRACES_HEADERS']) {
    const result = child(`const assert=require('node:assert/strict');
      assert.throws(() => require('@traceai/apollo-graphql').configure(), /OTEL_EXPORTER_OTLP/);`,
    { [variable]: 'Authorization=PRIVATE_TOKEN' });
    assert.equal(result.status, 0, result.stderr);
    assert.doesNotMatch(result.stderr, /PRIVATE_TOKEN/);
  }
});

test('loading any patched GraphQL submodule first must reject late initialization', () => {
  for (const subpath of ['graphql/execution/execute', 'graphql/language/parser', 'graphql/validation/validate']) {
    const result = child(`const assert=require('node:assert/strict');
      require(${JSON.stringify(subpath)});
      assert.throws(() => require('@traceai/apollo-graphql').configure(), /before importing/);`);
    assert.equal(result.status, 0, `${subpath}: ${result.stderr}`);
  }
});

test('traces-only setup leaves the process-wide logger provider unclaimed', () => {
  const result = child(`const assert=require('node:assert/strict');
    const {logs}=require('@opentelemetry/api-logs');
    const {configure}=require('@traceai/apollo-graphql');
    const telemetry=configure();
    const provider = { getLogger() { return {emit(){}}; } };
    logs.setGlobalLoggerProvider(provider);
    assert.equal(logs.getLoggerProvider(), provider);
    telemetry.shutdown();`);
  assert.equal(result.status, 0, result.stderr);
});

test('background exporter failure contains no response body in diagnostics or callbacks', async () => {
  const messages = [];
  diag.setLogger({ error: (...args) => messages.push(args), warn: (...args) => messages.push(args),
    debug() {}, info() {}, verbose() {} });
  try {
    const exporter = new FiSanitizingExporter({
      export(_spans, callback) {
        callback({ code: ExportResultCode.FAILED, error: new Error('PRIVATE_RESPONSE_BODY') });
      },
      shutdown: async () => {},
    });
    let result;
    exporter.export([], value => { result = value; diag.error(result.error); });
    assert.equal(result.code, ExportResultCode.FAILED);
    assert.doesNotMatch(JSON.stringify(messages.map(args => args.map(String))), /PRIVATE_RESPONSE_BODY/);
  } finally { diag.disable(); }
});

test('a silent exporter cannot hold shutdown open indefinitely', async () => {
  let stopped = false;
  const exporter = new FiSanitizingExporter({
    export() { /* transport never calls back */ },
    shutdown: async () => { stopped = true; },
  });
  exporter.export([], () => {});
  const outcome = await Promise.race([
    exporter.shutdown().then(() => 'success', () => 'failed'),
    new Promise(resolve => setTimeout(() => resolve('unbounded'), 4500)),
  ]);
  assert.equal(outcome, 'failed');
  assert.equal(stopped, true);
});

test('forceFlush awaits already-started background exports and reports their failure', async () => {
  let finish;
  const exporter = new FiSanitizingExporter({
    export(_spans, callback) { finish = callback; },
    shutdown: async () => {},
  });
  exporter.export([], () => {});
  const pending = exporter.forceFlush().then(() => 'success', () => 'failed');
  assert.equal(await Promise.race([pending, new Promise(resolve => setTimeout(() => resolve('pending'), 25))]), 'pending');
  finish({ code: ExportResultCode.FAILED, error: new Error('PRIVATE_RESPONSE_BODY') });
  assert.equal(await pending, 'failed');
});
