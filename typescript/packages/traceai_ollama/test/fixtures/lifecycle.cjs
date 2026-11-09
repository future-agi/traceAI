const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { createServer } = require('node:http');
const { once } = require('node:events');
const { pathToFileURL } = require('node:url');
const fs = require('node:fs');
const path = require('node:path');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = coreRequire('@opentelemetry/sdk-trace-base');
const [entrypoint, scenario] = process.argv.slice(2);

(async () => {
  // Exercise the public instrumentation build matching the SDK entrypoint.
  const { OllamaInstrumentation } = entrypoint === 'manual-esm'
    ? await import('@traceai/ollama') : require('@traceai/ollama');
  const exporters = [new InMemorySpanExporter(), new InMemorySpanExporter()];
  const providers = exporters.map(exporter => new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  }));
  const instrumentation = new OllamaInstrumentation({
    instrumentationConfig: { enabled: entrypoint === 'automatic-cjs' },
    traceConfig: { hideInputs: true },
  });
  instrumentation.setTracerProvider(providers[0]);
  let requests = 0;
  const server = createServer(async (req, res) => {
    for await (const chunk of req) { /* consume the real SDK request */ }
    requests++;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ model: 'offline-model', message: { role: 'assistant', content: 'done' },
      response: 'done', embedding: [0.1, 0.2], embeddings: [[0.1, 0.2]], done: true }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const host = `http://127.0.0.1:${server.address().port}`;
  try {
    const sdkEntry = require.resolve(process.env.OLLAMA_SDK_PATH || 'ollama');
    const sdkDir = path.dirname(path.dirname(sdkEntry));
    const manifest = JSON.parse(fs.readFileSync(path.join(sdkDir, 'package.json'), 'utf8'));
    const esmExport = manifest.exports?.['.']?.import;
    // SDK 0.4.0 is CJS-only; still exercise its namespace through native import().
    const importEntry = esmExport ? path.join(sdkDir,
      typeof esmExport === 'string' ? esmExport : esmExport.default) : sdkEntry;
    const sdk = entrypoint === 'manual-esm' ? await import(pathToFileURL(importEntry)) : require(sdkEntry);
    if (entrypoint !== 'automatic-cjs') instrumentation.manuallyInstrument(sdk);
    const singleton = typeof sdk.default.chat === 'function' ? sdk.default : sdk.default.default;
    singleton.config.host = host;
    const clients = [singleton, new sdk.Ollama({ host })];
    const methods = ['chat', 'generate', 'embed', 'embeddings'].filter(name => typeof clients[1][name] === 'function');
    async function invoke() {
      for (const client of clients) {
        for (const method of methods) {
          const result = await client[method]({ model: 'offline-model', stream: false,
            messages: [{ role: 'user', content: 'private-lifecycle-input' }],
            prompt: 'private-lifecycle-input', input: 'private-lifecycle-input' });
          assert.ok(result);
        }
      }
      await Promise.all(providers.map(provider => provider.forceFlush()));
    }
    const spansPerInvocation = clients.length * methods.length;
    const count = index => exporters[index].getFinishedSpans().length;
    await invoke();
    assert.equal(count(0), spansPerInvocation, 'baseline: one span per real request');
    if (scenario === 'provider') {
      instrumentation.setTracerProvider(providers[1]);
      await invoke();
      assert.equal(count(0), spansPerInvocation, 'old provider must not receive new spans');
      assert.equal(count(1), spansPerInvocation, 'new provider must receive every new span');
      await providers[0].shutdown();
      await invoke();
      assert.equal(count(1), 2 * spansPerInvocation, 'old provider shutdown must not affect new tracing');
    } else {
      const foreign = new Map();
      let foreignUnwraps = 0;
      if (scenario.endsWith('-wrapper')) {
        for (const method of methods) {
          const original = sdk.Ollama.prototype[method];
          const wrapper = function (...args) { return original.apply(this, args); };
          if (scenario === 'foreign-wrapper') {
            wrapper.__original = original;
            wrapper.__wrapped = true;
            wrapper.__unwrap = () => { foreignUnwraps++; sdk.Ollama.prototype[method] = original; };
          }
          sdk.Ollama.prototype[method] = wrapper;
          foreign.set(method, wrapper);
        }
      }
      for (let cycle = 0; cycle < 2; cycle++) {
        instrumentation.disable();
        instrumentation.disable(); // idempotent, even when auto hooks were never enabled
        if (entrypoint !== 'automatic-cjs') instrumentation.manuallyInstrument(sdk);
        const before = count(0);
        await invoke();
        assert.equal(count(0), before, 'disabled instrumentation must export zero new spans');
        instrumentation.enable();
        instrumentation.enable();
        await invoke();
        assert.equal(count(0), before + spansPerInvocation, 're-enable restores exactly one span per request');
        for (const [method, wrapper] of foreign) {
          assert.equal(sdk.Ollama.prototype[method], wrapper, 'lifecycle must preserve foreign wrapper identity');
        }
        assert.equal(foreignUnwraps, 0, 'lifecycle must never invoke foreign unwrap hooks');
      }
    }
    for (const exporter of exporters) {
      const attributes = exporter.getFinishedSpans().map(span => span.attributes);
      assert.ok(!JSON.stringify(attributes).includes('private-lifecycle-input'), 'lifecycle must retain traceConfig masking');
    }
    console.log(JSON.stringify({ entrypoint, scenario, sdk: manifest.version, requests,
      spans: exporters.map(exporter => exporter.getFinishedSpans().length) }));
  } finally {
    instrumentation.disable();
    await Promise.all(providers.map(provider => provider.shutdown()));
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
