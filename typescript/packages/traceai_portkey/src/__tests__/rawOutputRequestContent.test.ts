import { createServer, Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { InMemorySpanExporter, SimpleSpanProcessor, ReadableSpan } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { TraceConfigOptions } from '@traceai/fi-core';
import { PortkeyInstrumentation } from '../instrumentation';

// Loopback test with the real portkey-ai SDK. The Responses payload carries the request's system
// instructions in the SDK's own `instructions` field (OpenAI Response.instructions) and the request's
// tool definition in `tools`.
const sdk = require('portkey-ai');
const INSTRUCTIONS_SENTINEL = 'SYNTH-REQUEST-INSTRUCTIONS-6e3a';
const TOOL_SENTINEL = 'SYNTH-REQUEST-TOOL-DESCRIPTION-91fd';
const OUTPUT_SENTINEL = 'SYNTH-RESPONSE-TEXT-2b58';

const responseBody = {
  id: 'resp-1', object: 'response', created_at: 1, status: 'completed', model: 'returned-model',
  instructions: INSTRUCTIONS_SENTINEL,
  tools: [{ type: 'function', name: 'lookup', description: TOOL_SENTINEL, parameters: { type: 'object', properties: {} }, strict: false }],
  output: [{ type: 'message', id: 'msg-1', status: 'completed', role: 'assistant',
    content: [{ type: 'output_text', text: OUTPUT_SENTINEL, annotations: [] }] }],
  usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 },
};

describe('Portkey Responses raw output and request content', () => {
  let server: Server;
  let exporter: InMemorySpanExporter;
  let provider: NodeTracerProvider;
  let inst: PortkeyInstrumentation | undefined;

  beforeAll(async () => {
    server = createServer((req, res) => {
      req.resume();
      req.on('end', () => {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify(responseBody));
      });
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  });
  afterAll(async () => { await new Promise<void>(resolve => server.close(() => resolve())); });
  afterEach(async () => { inst?.disable(); await provider?.shutdown(); });

  const run = async (config?: TraceConfigOptions): Promise<ReadableSpan[]> => {
    exporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const client = new sdk.Portkey({
      apiKey: 'SYNTHETIC-KEY', maxRetries: 0, baseURL: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`,
    });
    inst = new PortkeyInstrumentation({ traceConfig: config });
    inst.setTracerProvider(provider);
    inst.manuallyInstrument(client);
    await client.responses.create({ model: 'requested-model', input: 'hello', instructions: INSTRUCTIONS_SENTINEL });
    await provider.forceFlush();
    return exporter.getFinishedSpans();
  };
  const dump = (spans: ReadableSpan[]) =>
    JSON.stringify(spans.map(s => ({ attributes: s.attributes, events: s.events, links: s.links })));

  it('records the response when nothing is hidden (control)', async () => {
    const spans = await run();
    expect(spans.length).toBeGreaterThan(0);
    expect(dump(spans)).toContain(OUTPUT_SENTINEL);
    expect(spans.some(s => typeof s.attributes['raw.output'] === 'string')).toBe(true);
  });

  it.each([
    ['hideInputs', { hideInputs: true }],
    ['hideInputMessages', { hideInputMessages: true }],
    ['hideInputText', { hideInputText: true }],
  ] as [string, TraceConfigOptions][])('rawOutputOmitsRequestInstructionsUnderInputSettings (%s)', async (_name, config) => {
    const spans = await run(config);
    expect(spans.length).toBeGreaterThan(0);
    const all = dump(spans);
    expect(all).not.toContain(INSTRUCTIONS_SENTINEL);
    expect(all).not.toContain(TOOL_SENTINEL);
  });
});
