const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { createServer } = require('node:http');
const { once } = require('node:events');
const { pathToFileURL } = require('node:url');
const fs = require('node:fs');
const path = require('node:path');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = coreRequire('@opentelemetry/sdk-trace-base');
const { OllamaInstrumentation } = require('../../dist/cjs');
const mode = process.argv[2];

(async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new OllamaInstrumentation({ instrumentationConfig: { enabled: mode === 'automatic' } });
  instrumentation.setTracerProvider(provider);
  let requests = 0;
  const server = createServer(async (req, res) => {
    for await (const chunk of req) { /* consume the real SDK request */ }
    requests++;
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ model: 'offline-model', message: { role: 'assistant', content: 'done' },
      response: 'done', embedding: [0.1, 0.2], embeddings: [[0.1, 0.2]], done: true,
      prompt_eval_count: 3, eval_count: 2 }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const host = `http://127.0.0.1:${server.address().port}`;
  const sdkEntry = require.resolve(process.env.OLLAMA_SDK_PATH || 'ollama');
  const sdkDir = path.dirname(path.dirname(sdkEntry));
  const manifest = JSON.parse(fs.readFileSync(path.join(sdkDir, 'package.json'), 'utf8'));
  const cjs = require(sdkEntry);
  const esmExport = manifest.exports?.['.']?.import;
  const esm = esmExport ? await import(pathToFileURL(path.join(sdkDir,
    typeof esmExport === 'string' ? esmExport : esmExport.default))) : cjs;
  const selected = mode === 'manual-esm' ? esm : cjs;
  const patch = module => instrumentation.manuallyInstrument(module);
  async function invoke(client) {
    for (const method of ['chat', 'generate', 'embed', 'embeddings']) {
      if (typeof client[method] !== 'function') continue; // embed was added after 0.4.
      const result = await client[method]({ model: 'offline-model', stream: false,
        messages: [{ role: 'user', content: 'hello' }], prompt: 'hello', input: 'hello' });
      assert.ok(result);
    }
  }
  try {
    if (['mixed-classes', 'unpatch-one'].includes(mode)) {
      const first = { Ollama: cjs.Ollama };
      const second = { Ollama: esm.Ollama };
      patch(first); patch(second);
      await invoke(new cjs.Ollama({ host }));
      await invoke(new esm.Ollama({ host }));
      assert.equal(exporter.getFinishedSpans().length, requests, 'both distinct SDK classes export every request');
      if (mode === 'unpatch-one' && cjs.Ollama !== esm.Ollama) {
        instrumentation.unpatch(first);
        exporter.reset(); requests = 0;
        await invoke(new esm.Ollama({ host }));
        assert.equal(exporter.getFinishedSpans().length, requests, 'unpatching CJS must leave ESM instrumented');
        const count = requests;
        await invoke(new cjs.Ollama({ host }));
        assert.equal(exporter.getFinishedSpans().length, count, 'unpatched CJS no longer emits spans');
      }
    } else if (mode === 'foreign-wrapper') {
      const module = { Ollama: cjs.Ollama };
      patch(module);
      const original = cjs.Ollama.prototype.chat;
      const replacement = function (...args) { return original.apply(this, args); };
      let foreignUnwraps = 0;
      replacement.__original = original;
      replacement.__wrapped = true;
      replacement.__unwrap = () => { foreignUnwraps++; cjs.Ollama.prototype.chat = original; };
      cjs.Ollama.prototype.chat = replacement;
      instrumentation.disable();
      assert.equal(cjs.Ollama.prototype.chat, replacement, 'disable must not remove another owner\'s wrapper');
      assert.equal(foreignUnwraps, 0);
    } else {
      if (!['automatic', 'unpatched'].includes(mode)) patch(selected);
      if (mode === 'repeated') {
        patch(selected);
        patch(Object.freeze({ Ollama: selected.Ollama, default: selected.default }));
        const other = new OllamaInstrumentation({ instrumentationConfig: { enabled: false } });
        other.setTracerProvider(provider);
        other.manuallyInstrument(selected);
        other.disable(); // not the owner of these wrappers
      }
      selected.default.config.host = host;
      // Assert separately: singleton success must not conceal missing class spans.
      for (const client of [selected.default, new selected.Ollama({ host }), new selected.Ollama({ host })]) {
        await invoke(client);
        assert.equal(exporter.getFinishedSpans().length, mode === 'unpatched' ? 0 : requests,
          'each default/new-client request emits exactly one span');
      }
    }
    console.log(JSON.stringify({ mode, sdk: manifest.version, distinctClasses: cjs.Ollama !== esm.Ollama,
      requests, spans: exporter.getFinishedSpans().length }));
  } finally {
    instrumentation.disable();
    await provider.shutdown();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
