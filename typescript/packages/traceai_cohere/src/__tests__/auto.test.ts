import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve } from "node:path";

const run = promisify(execFile);
const cwd = resolve(__dirname, "../..");

it("automatically instruments a fresh CommonJS SDK import", async () => {
  // Isolate the require hook from Jest's module loader; no SDK methods are mocked.
  const source = `
    const { InMemorySpanExporter, SimpleSpanProcessor, BasicTracerProvider } = require('@opentelemetry/sdk-trace-base');
    const { CohereInstrumentation } = require('./dist/src/index.js');
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new CohereInstrumentation();
    instrumentation.setTracerProvider(provider);
    const { CohereClientV2 } = require('cohere-ai');
    const client = new CohereClientV2({ token: 'offline-placeholder', fetcher: async () => ({
      ok: true, rawResponse: { status: 200 }, body: {
        id: 'fixture', finish_reason: 'COMPLETE', message: { role: 'assistant', content: [{ type: 'text', text: 'hello' }] }
      }
    }) });
    (async () => {
      await client.chat({ model: 'command-test', messages: [{ role: 'user', content: 'question' }] });
      const spans = exporter.getFinishedSpans();
      if (spans.length !== 1) throw new Error('Expected one automatically instrumented span, got ' + spans.length);
      instrumentation.disable();
      await client.chat({ model: 'command-test', messages: [] });
      if (exporter.getFinishedSpans().length !== 1) throw new Error('Disabled bound method still traced');
      await provider.shutdown();
      console.log('automatic-v2-ok');
    })().catch((error) => { console.error(error); process.exitCode = 1; });
  `;
  const { stdout } = await run(process.execPath, ["-e", source], { cwd });
  expect(stdout).toContain("automatic-v2-ok");
});

it("loads the native ESM package entry and manually instruments a real v2 client", async () => {
  const source = `
    import { CohereInstrumentation } from './dist/esm/index.js';
    import * as Cohere from 'cohere-ai';
    import { InMemorySpanExporter, SimpleSpanProcessor, BasicTracerProvider } from '@opentelemetry/sdk-trace-base';
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new CohereInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(Cohere);
    const client = new Cohere.CohereClientV2({ token: 'offline-placeholder', fetcher: async () => ({
      ok: true, rawResponse: { status: 200 }, body: {
        id: 'fixture', finish_reason: 'COMPLETE', message: { role: 'assistant', content: [{ type: 'text', text: 'hello' }] }
      }
    }) });
    await client.chat({ model: 'command-test', messages: [{ role: 'user', content: 'question' }] });
    if (exporter.getFinishedSpans().length !== 1) throw new Error('Missing ESM v2 span');
    instrumentation.disable();
    await provider.shutdown();
    console.log('esm-v2-ok');
  `;
  const { stdout } = await run(process.execPath, ["--input-type=module", "-e", source], { cwd });
  expect(stdout).toContain("esm-v2-ok");
});
