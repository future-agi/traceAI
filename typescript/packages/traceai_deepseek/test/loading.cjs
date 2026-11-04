const { test } = require('node:test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
const sdkPath = require.resolve(process.env.OPENAI_TEST_MODULE || 'openai');
for (const mode of ['automatic-cjs', 'manual-esm']) {
  test(`${mode} registers a real SDK resource and exports exactly one span`, () => {
    const instrumentor = pathToFileURL(path.join(root, 'dist', mode === 'manual-esm' ? 'esm' : 'cjs', 'index.js')).href;
    const sdkModule = pathToFileURL(path.join(path.dirname(sdkPath), 'index.mjs')).href;
    const script = `
      import assert from 'node:assert/strict';
      import { createRequire } from 'node:module';
      const require = createRequire(${JSON.stringify(path.join(root, 'package.json'))});
      const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
      const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
      const { DeepSeekInstrumentation, isPatched } = await import(${JSON.stringify(instrumentor)});
      const exporter = new InMemorySpanExporter();
      const provider = new NodeTracerProvider({spanProcessors:[new SimpleSpanProcessor(exporter)]});
      provider.register();
      const instrumentation = new DeepSeekInstrumentation();
      instrumentation.setTracerProvider(provider);
      const sdk = ${mode === 'manual-esm' ? `await import(${JSON.stringify(sdkModule)})` : `require(${JSON.stringify(sdkPath)})`};
      ${mode === 'manual-esm' ? 'instrumentation.manuallyInstrument(sdk);' : ''}
      const OpenAI = sdk.OpenAI || sdk.default || sdk;
      const client = new OpenAI({apiKey:'synthetic-not-a-key', baseURL:'https://api.deepseek.com/v1', maxRetries:0,
        fetch:async () => new Response(JSON.stringify({id:'test',model:'deepseek-chat',choices:[{index:0,message:{role:'assistant',content:'test'},finish_reason:'stop'}]}), {headers:{'content-type':'application/json'}})});
      await client.chat.completions.create({model:'deepseek-chat',messages:[{role:'user',content:'test'}]});
      await provider.forceFlush();
      assert.equal(isPatched(), true);
      assert.equal(exporter.getFinishedSpans().length, 1);
      instrumentation.disable();
      assert.equal(isPatched(), false);
      await provider.shutdown();
    `;
    execFileSync(process.execPath, ['--input-type=module', '-e', script], { stdio: 'pipe' });
  });
}
