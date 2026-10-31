import { createServer, Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { PortkeyInstrumentation } from '../instrumentation';
import { TraceConfigOptions } from '@traceai/fi-core';

const marker = 'private-content-canary';
const sdks = [require('portkey-floor'), require('portkey-v1'), require('portkey-v2'), require('portkey-ai')];
let server: Server;
let provider: NodeTracerProvider;
let wire: any[] = [];
let inst: PortkeyInstrumentation;
let fetchMock: jest.SpyInstance;

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      expect(req.url).toBe('/v1/traces');
      wire.push(JSON.parse(Buffer.concat(chunks).toString()));
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{}');
    });
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/traces`,
  }))] });
});
beforeEach(() => { wire = []; });
afterEach(() => { inst?.disable(); fetchMock?.mockRestore(); });
afterAll(async () => { await provider.shutdown(); await new Promise<void>(resolve => server.close(() => resolve())); });

const settings: TraceConfigOptions[] = [
  { hideInputs: true, hideOutputs: true },
  { hideInputMessages: true, hideOutputMessages: true },
  { hideInputText: true, hideOutputText: true },
];
it.each(sdks.flatMap((sdk, index) => settings.map(config => [index, sdk, config] as const)))(
  'redacts content on the OTLP wire for SDK matrix entry %s', async (_index, sdk, config) => {
    fetchMock = jest.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({
      id: 'synthetic-id', model: 'synthetic-model',
      choices: [{ message: { role: 'assistant', content: marker, tool_calls: [{ function: { name: 'tool', arguments: marker } }] } }],
      usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
    }), { headers: { 'content-type': 'application/json' } }));
    inst = new PortkeyInstrumentation({ traceConfig: config });
    inst.setTracerProvider(provider);
    inst.manuallyInstrument(sdk);
    const client = new sdk.Portkey({ apiKey: 'credential-canary', baseURL: 'http://localhost:9999' });
    const input = { model: 'synthetic-model', messages: [{ role: 'user', content: marker }] };
    await client.chat.completions.create(input);
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ error: { message: marker } }), { status: 400, headers: { 'content-type': 'application/json' } }));
    await expect(client.chat.completions.create(input)).rejects.toBeDefined();
    await provider.forceFlush();
    const serialized = JSON.stringify(wire);
    expect(serialized).not.toContain(marker);
    expect(serialized).not.toContain('credential-canary');
    const spans = wire.flatMap(body => body.resourceSpans.flatMap((r: any) => r.scopeSpans.flatMap((s: any) => s.spans)));
    expect(spans).toHaveLength(2);
    expect(spans[0].attributes).toEqual(expect.arrayContaining([
      { key: 'gen_ai.provider.name', value: { stringValue: 'portkey' } },
      { key: 'gen_ai.usage.input_tokens', value: { intValue: 2 } },
    ]));
    expect(spans[1].status.code).toBe(2);
    expect(spans[1].status.message).toBeUndefined();
  },
);

it('exports opt-in messages, tool arguments, and response usage as OTLP attributes', async () => {
  const message = { role: 'assistant', content: null, tool_calls: [{ id: 'call-id', type: 'function', function: { name: 'weather', arguments: '{"city":"Paris"}' } }] };
  fetchMock = jest.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({
    id: 'synthetic-id', model: 'response-model', choices: [{ message, finish_reason: 'tool_calls' }],
    usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
  }), { headers: { 'content-type': 'application/json' } }));
  inst = new PortkeyInstrumentation({ traceConfig: { hideInputs: false, hideOutputs: false } });
  inst.setTracerProvider(provider);
  const sdk = sdks[3];
  inst.manuallyInstrument(sdk);
  const client = new sdk.Portkey({ apiKey: 'credential-canary', baseURL: 'http://localhost:9999' });
  await client.chat.completions.create({ model: 'test', messages: [{ role: 'user', content: 'weather?' }] });
  await provider.forceFlush();
  const [span] = wire[0].resourceSpans[0].scopeSpans[0].spans;
  const attrs = Object.fromEntries(span.attributes.map((attribute: any) => [attribute.key, attribute.value.stringValue ?? attribute.value.intValue]));
  expect(JSON.parse(attrs['gen_ai.output.messages'])).toEqual([message]);
  expect(JSON.parse(attrs['gen_ai.input.messages'])).toEqual([{ role: 'user', content: 'weather?' }]);
  expect(attrs['gen_ai.response.model']).toBe('response-model');
  expect(JSON.stringify(wire)).not.toContain('credential-canary');
});

it('hides streamed content and tools without dropping token usage on the wire', async () => {
  fetchMock = jest.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(
    `data: ${JSON.stringify({ choices: [{ delta: { content: marker, tool_calls: [{ index: 0, function: { name: 'tool', arguments: marker } }] } }], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } })}\n\ndata: [DONE]\n\n`,
    { headers: { 'content-type': 'text/event-stream' } },
  ));
  inst = new PortkeyInstrumentation({ traceConfig: { hideInputs: true, hideOutputs: true } });
  inst.setTracerProvider(provider);
  const sdk = sdks[3];
  inst.manuallyInstrument(sdk);
  const client = new sdk.Portkey({ apiKey: 'credential-canary', baseURL: 'http://localhost:9999' });
  for await (const _ of await client.chat.completions.create({ model: 'test', messages: [{ role: 'user', content: marker }], stream: true })) { /* consume */ }
  await provider.forceFlush();
  expect(JSON.stringify(wire)).not.toContain(marker);
  expect(JSON.stringify(wire)).toContain('gen_ai.usage.input_tokens');
});
