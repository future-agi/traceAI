const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { pathToFileURL } = require('node:url');
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { spawnSync } = require('node:child_process');
const { buildSync } = require('esbuild');
const http = require('node:http');
const Module = require('node:module');
const { createClient } = require('./transport.cjs');
const packageDir = join(__dirname, '..');

test('README example stays executable and exports real client events over OTLP', { timeout: 10000 }, async t => {
  const readme = readFileSync(join(packageDir, 'README.md'), 'utf8');
  const source = readme.match(/```typescript\n([\s\S]*?)```/)[1];
  assert.equal(source, readFileSync(join(packageDir, 'examples/example.ts'), 'utf8'));
  const build = buildSync({
    stdin: { contents: source, loader: 'ts', resolveDir: packageDir },
    bundle: true, packages: 'external', platform: 'node', format: 'cjs', write: false,
    alias: { '@traceai/fi-instrumentation-pipecat/client': join(packageDir, 'dist/src/client.js') },
  });
  const example = new Module(join(__dirname, 'readme-example.cjs'), module);
  example.paths = module.paths;
  example._compile(build.outputFiles[0].text, join(__dirname, 'readme-example.cjs'));
  const received = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => { received.push(JSON.parse(Buffer.concat(chunks))); res.end('{}'); });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeIdleConnections(); }));
  const { client } = createClient(require('@pipecat-ai/client-js'));
  const stop = example.exports.traceClient(client, `http://127.0.0.1:${server.address().port}/v1/traces`);
  await client.connect();
  await client.sendText('Hello');
  await client.disconnect();
  await stop();
  const spans = received.flatMap(r => r.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
  assert.equal(spans.filter(s => s.name === 'Pipecat Client Event: botReady').length, 1);
  assert.equal(spans.filter(s => s.name === 'Pipecat Client Event: disconnected').length, 1);
  assert.equal(client.listenerCount('botReady'), 0);
});

test('browser entry bundles without Node or FI credential dependencies', () => {
  const bundle = buildSync({
    entryPoints: [join(packageDir, 'dist/src/client.js')],
    bundle: true, platform: 'browser', format: 'esm', write: false, metafile: true,
  });
  const inputs = Object.keys(bundle.metafile.inputs).join('\n');
  assert.doesNotMatch(inputs, /fi-core|opentelemetry[+/]instrumentation|grpc/);
  assert.doesNotMatch(bundle.outputFiles[0].text, /FI_API_KEY|FI_SECRET_KEY|process\.env/);
});

test('automatic CommonJS instrumentation patches current SDK once', () => {
  const child = spawnSync(process.execPath, ['-e', `
    const assert = require('node:assert/strict');
    const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
    const { PipecatInstrumentation } = require('./dist/src');
    const { createClient } = require('./test/transport.cjs');
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({spanProcessors: [new SimpleSpanProcessor(exporter)]});
    const inst = new PipecatInstrumentation();
    inst.setTracerProvider(provider);
    const sdk = require('@pipecat-ai/client-js');
    const { client } = createClient(sdk);
    (async () => {
      await client.connect(); await client.sendText('private'); await client.disconnect();
      assert.equal(exporter.getFinishedSpans().length, 3);
      inst.disable(); await provider.shutdown();
    })().catch(error => { console.error(error); process.exitCode = 1; });
  `], { cwd: packageDir, encoding: 'utf8', timeout: 10000 });
  assert.equal(child.status, 0, child.stdout + child.stderr);
});

test('ESM consumers export spans through immutable namespaces on both Node generations', async t => {
  // Upstream 1.16.0 publishes ESM as .js without type:module. Older Node needs
  // its public CommonJS entry via createRequire; Node 22+ detects ESM syntax.
  const interop = `
    import { createRequire } from 'node:module';
    const sdk = createRequire(${JSON.stringify(pathToFileURL(__filename).href)})('@pipecat-ai/client-js');
    export const { PipecatClient, Transport, RTVIMessage } = sdk;
  `;
  const sdk = Number(process.versions.node.split('.')[0]) >= 22
    ? await import('@pipecat-ai/client-js')
    : await import('data:text/javascript,' + encodeURIComponent(interop));
  const entry = await import('../dist/src/index.js');
  const browserEntry = await import('../dist/src/client.js');
  assert.equal(typeof browserEntry.observePipecatClient, 'function');
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const inst = new entry.PipecatInstrumentation();
  inst.setTracerProvider(provider);
  t.after(async () => { inst.disable(); await provider.shutdown(); });
  assert.equal(Object.isExtensible(sdk), false);
  inst.manuallyInstrument(sdk);
  const { client } = createClient(sdk);
  await client.connect();
  await client.disconnect();
  assert.equal(exporter.getFinishedSpans().length, 2);
  assert.ok(exporter.getFinishedSpans().every(s => s.attributes['pipecat.telemetry_source'] === 'client'));
});
