import { PortkeyInstrumentation } from '../instrumentation';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { SemanticConventions as S } from '@traceai/fi-semantic-conventions';

const sdks = [
  ['1.0.0', require('portkey-floor')],
  ['1.10.4', require('portkey-v1')],
  ['2.0.2', require('portkey-v2')],
  ['3.1.0', require('portkey-ai')],
] as const;

const reply = {
  id: 'chat-synthetic', model: 'synthetic-model',
  choices: [{ index: 0, message: { role: 'assistant', content: 'hello' }, finish_reason: 'stop' }],
  usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
};
const request = { model: 'synthetic-model', messages: [{ role: 'user', content: 'hello' }] };

describe.each(sdks)('real portkey-ai %s', (_version, sdk) => {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let inst: PortkeyInstrumentation;
  let fetchMock: jest.SpyInstance;
  beforeEach(() => {
    exporter.reset();
    // The real SDK captures global fetch when constructing a client. No model API is called.
    fetchMock = jest.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(reply), {
      headers: { 'content-type': 'application/json' },
    }));
    inst = new PortkeyInstrumentation();
    inst.setTracerProvider(provider);
    inst.manuallyInstrument(sdk);
  });
  afterEach(() => { inst.disable(); fetchMock.mockRestore(); });
  afterAll(async () => { await provider.shutdown(); });

  const client = () => new sdk.Portkey({ apiKey: 'synthetic-key', baseURL: 'http://localhost:9999/v1' });
  const respond = (body: any) => fetchMock.mockImplementation(async () => new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json' },
  }));
  const stream = (chunks: any[]) => fetchMock.mockImplementation(async () => new Response(
    chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n',
    { headers: { 'content-type': 'text/event-stream' } },
  ));

  it('preserves tool calls, usage, and request options without exporting credentials', async () => {
    const message = { role: 'assistant', content: null, tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'weather', arguments: '{"city":"Paris"}' } }] };
    respond({ ...reply, choices: [{ message, finish_reason: 'tool_calls' }] });
    const result = await client().chat.completions.create(request, { provider: 'openai' }, { headers: { 'x-synthetic': 'private-header-value' } });
    expect(result.choices[0].message).toEqual(message);
    const [span] = exporter.getFinishedSpans();
    expect(JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES] as string)[0]).toEqual(message);
    expect(JSON.stringify(span.attributes)).not.toContain('synthetic-key');
    expect(JSON.stringify(span.attributes)).not.toContain('private-header-value');
  });

  it('traces text completions and embeddings without exporting vectors', async () => {
    const c = client();
    respond({ ...reply, choices: [{ text: 'completed', finish_reason: 'stop' }] });
    await c.completions.create({ model: 'synthetic-model', prompt: 'input' });
    respond({ data: [{ embedding: [0.123456789, 0.987654321] }], usage: { prompt_tokens: 2, total_tokens: 2 } });
    await c.embeddings.create({ model: 'embedding-model', input: ['a', 'b'] });
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(2);
    expect(spans[0].attributes[S.OUTPUT_VALUE]).toBe('completed');
    expect(spans[1].attributes[S.FI_SPAN_KIND]).toBe('EMBEDDING');
    expect(spans[1].attributes['embedding.vector_count']).toBe(1);
    expect(spans[1].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(2);
    expect(JSON.stringify(spans[1].attributes)).not.toContain('0.123456789');
  });

  it('aggregates streamed tools, content, model and non-final usage', async () => {
    stream([
      { id: 'stream-id', model: 'returned-model', choices: [{ index: 0, delta: { content: 'hel', tool_calls: [{ index: 0, id: 'call-1', function: { name: 'weather', arguments: '{"city":' } }] } }] },
      { choices: [{ index: 0, delta: { content: 'lo', tool_calls: [{ index: 0, function: { arguments: '"Paris"}' } }] }, finish_reason: 'tool_calls' }], usage: reply.usage },
      { choices: [] },
    ]);
    const result = await client().chat.completions.create({ ...request, stream: true });
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    const chunks = [];
    for await (const chunk of result) chunks.push(chunk);
    expect(chunks).toHaveLength(3);
    const [span] = exporter.getFinishedSpans();
    expect(span.attributes[S.OUTPUT_VALUE]).toBe('hello');
    expect(span.attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(3);
    expect(span.attributes[S.GEN_AI_RESPONSE_MODEL]).toBe('returned-model');
    expect(JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES] as string)[0].tool_calls[0].function.arguments).toBe('{"city":"Paris"}');
  });

  it('finishes text streams and early chat iterator exits', async () => {
    stream([{ choices: [{ index: 0, text: 'text' }] }, { choices: [{ index: 0, text: ' end', finish_reason: 'stop' }], usage: reply.usage }]);
    const c = client();
    for await (const _ of await c.completions.create({ model: 'test', prompt: 'input', stream: true })) { /* consume */ }
    expect(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE]).toBe('text end');
    stream([{ choices: [{ index: 0, delta: { content: 'first' } }] }, { choices: [] }]);
    for await (const _ of await c.chat.completions.create({ ...request, stream: true })) break;
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it('records request errors and propagates rejection', async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ error: { message: 'synthetic failure' } }), { status: 400, headers: { 'content-type': 'application/json' } }));
    await expect(client().chat.completions.create(request)).rejects.toBeDefined();
    const [span] = exporter.getFinishedSpans();
    expect(span.status.code).toBe(2);
    expect(span.events.some(event => event.name === 'exception')).toBe(true);
  });

  it('supports pre-existing clients, repeated registration, disable and re-enable', async () => {
    inst.disable();
    const c = client();
    inst.enable();
    inst.manuallyInstrument(c);
    inst.manuallyInstrument(c);
    await c.chat.completions.create(request);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    inst.disable();
    await c.chat.completions.create(request);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    inst.enable();
    await c.chat.completions.create(request);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  if (_version !== '1.0.0') {
    const responseBody = { id: 'resp-test', model: 'returned-model', status: 'completed', output: [{ type: 'function_call', call_id: 'tool-id', name: 'weather', arguments: '{"city":"Paris"}' }], usage: { input_tokens: 4, output_tokens: 2, total_tokens: 6 } };
    const responseMessages = [{ role: 'assistant', tool_calls: [{ id: 'tool-id', type: 'function', function: { name: 'weather', arguments: '{"city":"Paris"}' } }] }];
    it('retains partial Responses output on early stream exit', async () => {
      stream([{ type: 'response.output_text.delta', output_index: 0, delta: 'partial text' }, { type: 'response.completed', response: responseBody }]);
      for await (const _ of await client().responses.create({ model: 'test', input: 'hello', stream: true })) break;
      expect(exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE]).toBe('partial text');
    });
    it('traces Responses withResponse without changing its SDK helper contract', async () => {
      respond(responseBody);
      const promise = client().responses.create({ model: 'test', input: 'hello' });
      expect(typeof promise.withResponse).toBe('function');
      expect(typeof promise.asResponse).toBe('function');
      const { data, response } = await promise.withResponse();
      expect(data.output).toEqual(responseBody.output);
      expect(response.status).toBe(200);
      expect((await promise).id).toBe('resp-test');
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(spans[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(6);
      expect(JSON.parse(spans[0].attributes[S.LLM_OUTPUT_MESSAGES] as string)).toEqual(responseMessages);
    });
    it('does not consume the raw Responses body returned by asResponse', async () => {
      respond(responseBody);
      const raw = await client().responses.create({ model: 'test', input: 'hello' }).asResponse();
      expect(raw.bodyUsed).toBe(false);
      expect((await raw.json()).id).toBe('resp-test');
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });
    it('preserves Responses stream conversion helpers and closes the span', async () => {
      stream([{ type: 'response.completed', response: responseBody }]);
      const response = await client().responses.create({ model: 'test', input: 'hello', stream: true });
      const readable = response.toReadableStream();
      await new Response(readable).text();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    });
    it('traces streamed Responses completion and tool output', async () => {
      stream([{ type: 'response.created', response: { id: 'resp-test', model: 'returned-model' } }, { type: 'response.completed', response: responseBody }]);
      for await (const _ of await client().responses.create({ model: 'test', input: 'hello', stream: true })) { /* consume */ }
      const [span] = exporter.getFinishedSpans();
      expect(span.attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(6);
      expect(JSON.parse(span.attributes[S.LLM_OUTPUT_MESSAGES] as string)).toEqual(responseMessages);
    });
  }

  it('traces a real instance resource exactly once', async () => {
    const client = new sdk.Portkey({ apiKey: 'synthetic-key', baseURL: 'http://localhost:9999/v1' });
    expect(sdk.Portkey.prototype.chat).toBeUndefined();
    const result = await client.chat.completions.create(request);
    expect(result.choices[0].message.content).toBe('hello');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes[S.LLM_PROVIDER]).toBe('portkey');
    expect(spans[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(3);
  });
});
