// Run the actual documented snippet as native ESM. Only replace package paths,
// the network destination, and the exporter; do not add instrumentation steps.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createRequire } = require('node:module');
const { createServer } = require('node:http');
const { once } = require('node:events');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { SimpleSpanProcessor, InMemorySpanExporter } = coreRequire('@opentelemetry/sdk-trace-base');

(async () => {
  let requests = 0;
  const server = createServer(async (req, res) => {
    assert.equal(req.url, '/api/chat');
    for await (const chunk of req) { /* consume the real SDK request */ }
    requests++;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ model: 'llama3.2', message: { role: 'assistant', content: 'done' }, done: true }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const host = `http://127.0.0.1:${server.address().port}`;
  const exporter = new InMemorySpanExporter();
  globalThis.readmeTest = {
    spanProcessors: [new SimpleSpanProcessor(exporter)],
    verify() {
      assert.equal(requests, 1, 'the documented real SDK call succeeds');
      assert.equal(exporter.getFinishedSpans().length, 1, 'the documented setup must emit one Ollama span');
      assert.equal(exporter.getFinishedSpans()[0].name, 'Ollama Chat');
    },
  };
  try {
    const sdkEntry = require.resolve(process.env.OLLAMA_SDK_PATH || 'ollama');
    const sdkDir = path.dirname(path.dirname(sdkEntry));
    const manifest = JSON.parse(fs.readFileSync(path.join(sdkDir, 'package.json'), 'utf8'));
    const esmExport = manifest.exports?.['.']?.import;
    const imports = {
      '@traceai/fi-core': require.resolve('@traceai/fi-core').replace('/dist/src/', '/dist/esm/'),
      '@traceai/ollama': require.resolve('@traceai/ollama').replace('/dist/cjs/', '/dist/esm/'),
      '@opentelemetry/instrumentation': require.resolve('@opentelemetry/instrumentation'),
      ollama: esmExport ? path.join(sdkDir, typeof esmExport === 'string' ? esmExport : esmExport.default) : sdkEntry,
    };
    const readme = fs.readFileSync(path.join(__dirname, '../../README.md'), 'utf8');
    let snippet = readme.match(/### With TraceAI Core\s+```typescript\n([\s\S]*?)```/)[1];
    for (const [name, target] of Object.entries(imports)) {
      snippet = snippet.replaceAll(`"${name}"`, JSON.stringify(pathToFileURL(target).href));
    }
    snippet = snippet.replace('projectName: "my-ollama-app",',
      `projectName: "my-ollama-app", headers: {}, endpoint: ${JSON.stringify(host)}, spanProcessors: globalThis.readmeTest.spanProcessors,`);
    snippet = snippet.replace('new Ollama()', `new Ollama({ host: ${JSON.stringify(host)} })`);
    snippet = snippet.replace('await tracerProvider.shutdown();',
      'await tracerProvider.forceFlush();\ntry { globalThis.readmeTest.verify(); } finally { await tracerProvider.shutdown(); }');
    await import(`data:text/javascript,${encodeURIComponent(snippet)}`);
    console.log(JSON.stringify({ sdk: manifest.version, requests, setup: 'README native ESM, no loader' }));
  } finally {
    delete globalThis.readmeTest;
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
