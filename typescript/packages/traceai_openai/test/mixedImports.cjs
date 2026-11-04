// Run against packed consumers at the SDK floor and latest, not workspace mocks.
const { test, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { writeFileSync } = require('node:fs');
const { pathToFileURL } = require('node:url');

const base = process.env.OPENAI_CONSUMER_DIR;
const req = createRequire(base + '/package.json');
const CJS = req('openai');
const { OpenAIInstrumentation, isPatched } = req('@traceai/openai');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req('@opentelemetry/sdk-trace-base');
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const instances = [];
writeFileSync(base + '/mixed-sdk.mjs', 'import OpenAI from "openai"; export { OpenAI };\n');
const load = () => import(pathToFileURL(base + '/mixed-sdk.mjs').href);

function instrument() {
  const inst = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false } });
  inst.setTracerProvider(provider);
  instances.push(inst);
  return inst;
}
async function call(SDK) {
  const client = new SDK({ apiKey: 'synthetic-key', maxRetries: 0, fetch: async () => new Response(JSON.stringify({
    id: 'chat', object: 'chat.completion', created: 1, model: 'test',
    choices: [{ index: 0, message: { role: 'assistant', content: 'hello' }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  }), { headers: { 'content-type': 'application/json' } }) });
  await client.chat.completions.create({ model: 'test', messages: [{ role: 'user', content: 'hello' }] });
}
afterEach(async () => {
  for (const inst of instances.splice(0)) inst.disable();
  await provider.forceFlush();
  exporter.reset();
});
after(() => provider.shutdown());

test('ESM-only control', async () => {
  const esm = await load();
  instrument().manuallyInstrument(esm.OpenAI);
  await call(esm.OpenAI);
  assert.equal(exporter.getFinishedSpans().length, 1);
});

for (const reverse of [false, true]) {
  test(`mixed SDKs, immutable namespace, duplicate alias, disable and repatch (reverse=${reverse})`, async () => {
    const esm = await load(); // A genuine immutable module namespace.
    const modules = reverse ? [esm, CJS] : [CJS, esm];
    const inst = instrument();
    for (const module of modules) inst.manuallyInstrument(module);
    inst.manuallyInstrument({ OpenAI: CJS.OpenAI }); // Same class through another namespace.
    inst.manuallyInstrument(esm.OpenAI);
    await call(CJS);
    await call(esm.OpenAI);
    assert.equal(exporter.getFinishedSpans().length, 2);
    assert.equal(isPatched(), true);
    inst.disable();
    assert.equal(isPatched(), false);
    await call(CJS);
    await call(esm.OpenAI);
    assert.equal(exporter.getFinishedSpans().length, 2, 'disable unpatches every SDK');
    for (const module of modules) inst.manuallyInstrument(module);
    await call(CJS);
    await call(esm.OpenAI);
    assert.equal(exporter.getFinishedSpans().length, 4);
  });
}

test('disabling one owner leaves the other SDK and its tracer intact', async () => {
  const esm = await load();
  const owner = instrument();
  const other = instrument();
  owner.manuallyInstrument(CJS);
  other.manuallyInstrument(CJS); // Not an owner of this class.
  other.manuallyInstrument(esm);
  other.disable();
  assert.equal(isPatched(), true);
  await call(CJS);
  await call(esm.OpenAI);
  assert.equal(exporter.getFinishedSpans().length, 1);
  owner.disable();
  assert.equal(isPatched(), false);
});
