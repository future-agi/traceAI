// Execute the documented standalone example in a real child process. A test
// runner's forced exit must not hide leaked native handles in customer scripts.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const { spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { test } = require('node:test');

const packageDir = process.env.LIVEKIT_INSTRUMENTATION_DIR || resolve(__dirname, '..');
const packageRequire = createRequire(resolve(packageDir, 'package.json'));
const sdkRequire = createRequire(resolve(process.env.LIVEKIT_COMPAT_DIR, 'package.json'));

test('README standalone example exports the failed connection and exits naturally', () => {
  const readme = readFileSync(resolve(packageDir, 'README.md'), 'utf8');
  const snippet = readme.match(/```javascript\n([\s\S]*?)\n```/)[1];
  const coreImport = 'const { register, ProjectType } = require("@traceai/fi-core");';
  const instrumentationImport = 'require("@traceai/fi-instrumentation-livekit")';
  const rtcImport = 'import("@livekit/rtc-node")';
  for (const expected of [coreImport, instrumentationImport, rtcImport]) {
    assert.ok(snippet.includes(expected), `README example must contain ${expected}`);
  }
  // Keep the application and its teardown unchanged. Replace only the tracing
  // transport and module resolution so this test needs no vendor/Cloud access.
  const inMemorySetup = `
    const assert = require('node:assert/strict');
    const coreRequire = require('node:module').createRequire(${JSON.stringify(packageRequire.resolve('@traceai/fi-core'))});
    const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = coreRequire('@opentelemetry/sdk-trace-base');
    const { ProjectType } = coreRequire('@traceai/fi-core');
    function register(options) {
      assert.equal(options.endpoint, 'https://api.futureagi.com/tracer/v1/traces');
      const exporter = new InMemorySpanExporter();
      const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
      const shutdown = provider.shutdown.bind(provider);
      provider.shutdown = async () => {
        const spans = exporter.getFinishedSpans();
        assert.deepEqual(spans.map(span => span.attributes['livekit.operation']), ['room.connect', 'room.disconnect']);
        assert.equal(spans[0].status.code, 2);
        await shutdown();
        console.log('README spans exported and provider shut down');
      };
      return provider;
    }
  `;
  const script = snippet.replace(coreImport, inMemorySetup)
    .replace(instrumentationImport, `require(${JSON.stringify(packageRequire.resolve('@traceai/fi-instrumentation-livekit'))})`)
    .replace(rtcImport, `import(${JSON.stringify(pathToFileURL(sdkRequire.resolve('@livekit/rtc-node')).href)})`);
  const result = spawnSync(process.execPath, ['--input-type=commonjs', '--eval', script], {
    env: { ...process.env, LIVEKIT_URL: 'not-a-url', LIVEKIT_TOKEN: 'synthetic-token' },
    encoding: 'utf8', timeout: 8000,
  });
  assert.ifError(result.error);
  assert.equal(result.signal, null, result.stderr);
  assert.equal(result.status, 1, `Expected the connection-error exit code, not a timeout: ${result.stderr}`);
  assert.match(result.stdout, /README spans exported and provider shut down/);
});
