import { createServer, Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { TraceConfigOptions } from '@traceai/fi-core';
import { SemanticConventions as S } from '@traceai/fi-semantic-conventions';
import { PortkeyInstrumentation } from '../instrumentation';

const sdks = [
  ['1.0.0', require('portkey-floor')], ['1.10.4', require('portkey-v1')],
  ['2.0.2', require('portkey-v2')], ['3.1.0', require('portkey-ai')],
] as const;
const chatInput = { model: 'requested-model', messages: [{ role: 'user', content: 'ORIGINAL-PROMPT' }] };
const chatReply = { id: 'chat-test', choices: [{ message: { role: 'assistant', content: 'legitimate-output' }, finish_reason: 'stop' }] };
const responseReply = { id: 'resp-test', status: 'completed', output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'legitimate-output' }] }] };
const inputDetails = { cached_tokens: 2, cache_write_tokens: 3, audio_tokens: 0 };
const outputDetails = { reasoning_tokens: 1, audio_tokens: 2 };
const chatUsage = { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, prompt_tokens_details: inputDetails, completion_tokens_details: outputDetails };
const responsesUsage = { input_tokens: 10, output_tokens: 5, total_tokens: 15, input_tokens_details: inputDetails, output_tokens_details: outputDetails };
const wait = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

// Real SDKs talk only to this loopback server, with synthetic credentials. Compare
// telemetry with the body actually sent, rather than reimplementing the SDK in a mock.
describe.each(sdks)('release regressions / portkey-ai %s', (version, sdk) => {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let server: Server;
  let inst: PortkeyInstrumentation;
  let client: any;
  let received: any[];
  let reply: any;
  let chunks: any[] | undefined;
  let status: number;
  let oldHideErrorDetails: string | undefined;

  beforeAll(async () => {
    server = createServer((req, res) => {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        received.push(JSON.parse(body || '{}'));
        res.writeHead(status, { 'content-type': chunks ? 'text/event-stream' : 'application/json', 'x-request-id': 'synthetic-request' });
        res.end(chunks ? chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n' : JSON.stringify(reply));
      });
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  });
  const setup = (traceConfig?: TraceConfigOptions) => {
    inst?.disable();
    inst = new PortkeyInstrumentation({ traceConfig });
    inst.setTracerProvider(provider);
    inst.manuallyInstrument(client);
  };
  beforeEach(() => {
    exporter.reset();
    oldHideErrorDetails = process.env.FI_HIDE_ERROR_DETAILS;
    delete process.env.FI_HIDE_ERROR_DETAILS;
    received = [];
    reply = chatReply;
    chunks = undefined;
    status = 200;
    client = new sdk.Portkey({ apiKey: 'SYNTHETIC-KEY', baseURL: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`, maxRetries: 0 });
    setup();
  });
  afterEach(() => {
    inst.disable();
    if (oldHideErrorDetails === undefined) delete process.env.FI_HIDE_ERROR_DETAILS;
    else process.env.FI_HIDE_ERROR_DETAILS = oldHideErrorDetails;
  });
  afterAll(async () => {
    await provider.shutdown();
    await new Promise<void>(resolve => server.close(() => resolve()));
  });
  const resource = (operation: string) => operation === 'chat' ? client.chat.completions : client[operation];
  const input = (operation: string, content: string): Record<string, any> => ({ model: 'requested-model', ...(
    operation === 'chat' ? { messages: [{ role: 'user', content }] } : operation === 'completions' ? { prompt: content } : { input: content }
  ) });
  const assertUsage = () => {
    const [span] = exporter.getFinishedSpans();
    expect(span.attributes).toMatchObject({
      [S.LLM_TOKEN_COUNT_PROMPT]: 10, [S.LLM_TOKEN_COUNT_COMPLETION]: 5, [S.LLM_TOKEN_COUNT_TOTAL]: 15,
      [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: 2, [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE]: 3,
      [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO]: 0, [S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO]: 2,
      [S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]: 1,
    });
    expect(JSON.stringify(span.attributes)).not.toContain('ORIGINAL-PROMPT');
    expect(JSON.stringify(span.attributes)).not.toContain('legitimate-output');
    expect(span.attributes[S.RAW_INPUT]).toBeUndefined();
    expect(span.attributes[S.RAW_OUTPUT]).toBeUndefined();
  };

  it.each(['chat', 'completions', 'embeddings', ...(version === '1.0.0' ? [] : ['responses'])])(
    'captures the effective %s body override without changing arguments or headers', async operation => {
      const original = input(operation, 'ORIGINAL-PROMPT');
      const effective: Record<string, any> = { ...input(operation, 'ACTUAL-PROMPT'), model: 'effective-model' };
      const options = { body: effective, headers: { 'x-synthetic-private': 'HEADER-CANARY' } };
      const before = JSON.stringify([original, options]);
      await resource(operation).create(original, undefined, options);
      expect(received).toEqual([effective]);
      expect(JSON.stringify([original, options])).toBe(before);
      const [span] = exporter.getFinishedSpans();
      expect(JSON.parse(span.attributes[S.INPUT_VALUE] as string)).toEqual(effective);
      expect(JSON.parse(span.attributes[S.RAW_INPUT] as string)).toEqual(effective);
      expect(span.attributes[S.LLM_MODEL_NAME]).toBe('effective-model');
      if (operation === 'chat') expect(JSON.parse(span.attributes[S.LLM_INPUT_MESSAGES] as string)).toEqual(effective.messages);
      if (operation === 'responses') expect(JSON.parse(span.attributes[S.LLM_INPUT_MESSAGES] as string)).toEqual([{ role: 'user', content: 'ACTUAL-PROMPT' }]);
      if (operation === 'embeddings') expect(span.attributes[S.EMBEDDING_MODEL_NAME]).toBe('effective-model');
      expect(JSON.stringify(span.attributes)).not.toMatch(/ORIGINAL-PROMPT|HEADER-CANARY|SYNTHETIC-KEY/);
    },
  );

  it('ignores inherited/non-enumerable body options, just like the SDK spread', async () => {
    const override = { ...chatInput, messages: [{ role: 'user', content: 'NOT-SENT' }] };
    const options = [Object.create({ body: override }), Object.defineProperty({}, 'body', { value: override })];
    for (const opts of options) await client.chat.completions.create(chatInput, undefined, opts);
    expect(received).toEqual([chatInput, chatInput]);
    for (const span of exporter.getFinishedSpans()) {
      expect(JSON.parse(span.attributes[S.RAW_INPUT] as string)).toEqual(chatInput);
    }
  });

  it('keeps gateway cache counts, zero values, and omits missing or non-numeric details', async () => {
    setup({ hideInputs: true, hideOutputs: true });
    reply = { ...chatReply, usage: {
      cache_read_input_tokens: 2, cache_creation_input_tokens: 3,
      completion_tokens_details: { reasoning_tokens: 'not-a-count' },
    } };
    await client.chat.completions.create(chatInput);
    const [fallback] = exporter.getFinishedSpans();
    expect(fallback.attributes[S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]).toBe(2);
    expect(fallback.attributes[S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE]).toBe(3);
    expect(fallback.attributes[S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]).toBeUndefined();
    expect(fallback.attributes[S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO]).toBeUndefined();
    reply.usage.prompt_tokens_details = { cached_tokens: 0, cache_write_tokens: 0 };
    await client.chat.completions.create(chatInput);
    const zero = exporter.getFinishedSpans()[1];
    expect(zero.attributes[S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]).toBe(0);
    expect(zero.attributes[S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE]).toBe(0);
  });

  it.each([false, true])('retains chat cost inputs when content is hidden (stream=%s)', async streaming => {
    setup({ hideInputs: true, hideOutputs: true });
    reply = { ...chatReply, usage: chatUsage };
    if (streaming) chunks = [
      { choices: [{ index: 0, delta: { content: 'legitimate-output' } }] },
      { choices: [], usage: chatUsage }, { choices: [] },
    ];
    const result = await client.chat.completions.create({ ...chatInput, stream: streaming });
    if (streaming) for await (const _ of result) { /* consume */ }
    assertUsage();
  });

  it.each(['chat', 'completions', 'embeddings'])('ends completed %s spans before delayed await', async operation => {
    const pending = resource(operation).create(input(operation, 'ORIGINAL-PROMPT'));
    await wait(100);
    const finishedBeforeAwait = exporter.getFinishedSpans().length;
    // Always settle the SDK request, even on the pre-fix regression run.
    const result = await pending;
    expect(received).toHaveLength(1);
    expect(finishedBeforeAwait).toBe(1);
    const [span] = exporter.getFinishedSpans();
    await pending.then((value: any) => expect(value).toBe(result));
    await pending.catch(() => { throw new Error('unexpected rejection'); });
    let finalized = false;
    await pending.finally(() => { finalized = true; });
    expect(finalized).toBe(true);
    expect(exporter.getFinishedSpans()).toEqual([span]);
    expect(received).toHaveLength(1);
  });

  it('ends an entirely unobserved native success', async () => {
    void client.chat.completions.create(chatInput);
    await wait(100);
    expect(received).toHaveLength(1);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it('ends an entirely unobserved native rejection without an orphan rejection', async () => {
    status = 400;
    reply = { error: { message: 'synthetic-failure' } };
    void client.chat.completions.create(chatInput);
    await wait(100);
    expect(received).toHaveLength(1);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(2);
  });

  it('preserves delayed rejection and error identity', async () => {
    status = 400;
    reply = { error: { message: 'synthetic-failure' } };
    const pending = client.chat.completions.create(chatInput);
    await wait(100);
    const finishedBeforeAwait = exporter.getFinishedSpans().length;
    const error = await pending.catch((value: unknown) => value);
    expect(error).toBeInstanceOf(Error);
    expect(finishedBeforeAwait).toBe(1);
    await expect(pending).rejects.toBe(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(2);
  });

  if (version !== '1.0.0') {
    it.each(([
      ['explicit option', undefined, true, true], ['environment', 'true', undefined, true],
      ['explicit opt-out', 'true', false, false], ['default', undefined, undefined, false],
    ] as const).flatMap(([name, env, option, hidden]) => [false, true].map(streaming => [name, env, option, hidden, streaming] as const)))(
      'failed Responses: %s (env=%s, option=%s, hidden=%s, stream=%s)', async (_name, env, option, hidden, streaming) => {
      if (env) process.env.FI_HIDE_ERROR_DETAILS = env;
      setup(option === undefined ? undefined : { hideErrorDetails: option });
      reply = { ...responseReply, status: 'failed', error: { code: 'server_error', message: 'ERROR-DETAIL-CANARY', details: 'ERROR-DETAIL-CANARY' } };
      if (streaming) chunks = [{ type: 'response.failed', response: reply }];
      const result = await client.responses.create({ model: 'requested-model', input: 'hello', stream: streaming });
      if (streaming) {
        const returned = [];
        for await (const chunk of result) returned.push(chunk);
        expect(returned).toEqual(chunks);
      } else expect(result).toEqual(reply);
      const [span] = exporter.getFinishedSpans();
      expect(span.status.code).toBe(2);
      const raw = JSON.parse(span.attributes[S.RAW_OUTPUT] as string);
      expect(raw.status).toBe('failed');
      expect(raw.error.code).toBe('server_error');
      expect(raw.output).toEqual(responseReply.output);
      expect(JSON.stringify(span.attributes)).toContain('legitimate-output');
      const exported = JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status });
      expect(exported.includes('ERROR-DETAIL-CANARY')).toBe(!hidden);
    });

    it.each([false, true])('retains Responses cost inputs when content is hidden (stream=%s)', async streaming => {
      setup({ hideInputs: true, hideOutputs: true });
      reply = { ...responseReply, usage: responsesUsage };
      if (streaming) chunks = [{ type: 'response.completed', response: reply }];
      const result = await client.responses.create({ model: 'requested-model', input: 'ORIGINAL-PROMPT', stream: streaming });
      if (streaming) for await (const _ of result) { /* consume */ }
      assertUsage();
    });

    it('retains lazy Responses raw-body helpers after the HTTP reply arrives', async () => {
      reply = responseReply;
      const pending = client.responses.create({ model: 'requested-model', input: 'hello' });
      await wait(100);
      const raw = await pending.asResponse();
      expect(raw.bodyUsed).toBe(false);
      expect(await raw.json()).toEqual(responseReply);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(received).toHaveLength(1);
    });
  }
});
