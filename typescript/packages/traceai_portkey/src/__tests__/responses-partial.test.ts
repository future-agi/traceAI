import { createServer, Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { TraceConfigOptions } from '@traceai/fi-core';
import { SemanticConventions as S } from '@traceai/fi-semantic-conventions';
import { PortkeyInstrumentation } from '../instrumentation';

// Responses is not available in portkey-ai 1.0.0. Exercise all supported
// Responses SDK generations with real HTTP/SSE and OTLP, not tracing mocks.
const sdks = [
  ['1.10.4', require('portkey-v1')], ['2.0.2', require('portkey-v2')], ['3.1.0', require('portkey-ai')],
] as const;
const partial = 'PARTIAL-CONTENT-CANARY person@example.com';
const errorDetail = 'ERROR-DETAIL-CANARY';
const credential = 'SYNTHETIC-KEY';
const textEvents = () => [
  { type: 'response.output_item.added', output_index: 0, item: { type: 'message', role: 'assistant', content: [] } },
  { type: 'response.output_text.delta', output_index: 0, delta: partial },
];
const terminal = (status = 'failed', output?: unknown[]) => ({
  type: `response.${status}`,
  response: {
    id: 'resp-partial', model: 'returned-model', status,
    ...(output === undefined ? {} : { output }),
    ...(status === 'failed' ? { error: { code: 'server_error', message: errorDetail, details: errorDetail } } : {}),
    ...(status === 'incomplete' ? { incomplete_details: { reason: 'max_output_tokens' } } : {}),
    usage: null,
  },
});
type WireAttributes = { key: string; value: { stringValue?: string; intValue?: string | number } }[];
interface WireSpan {
  attributes: WireAttributes;
  status: { code?: number; message?: string };
  events?: unknown[];
}
interface WireRequest { resourceSpans: { scopeSpans: { spans: WireSpan[] }[] }[] }
const attribute = (span: WireSpan, key: string) => span.attributes.find(item => item.key === key)?.value.stringValue;
const policies: { name: string; env?: string; config?: TraceConfigOptions; hideError: boolean; pii?: boolean }[] = [
  { name: 'default', hideError: false },
  { name: 'hide error option', config: { hideErrorDetails: true }, hideError: true },
  { name: 'hide error environment', env: 'true', hideError: true },
  { name: 'explicit opt-out', env: 'true', config: { hideErrorDetails: false }, hideError: false },
  { name: 'PII redaction', config: { piiRedaction: true, hideErrorDetails: true }, hideError: true, pii: true },
];

describe.each(sdks)('partial Responses / portkey-ai %s', (_version, sdk) => {
  let server: Server;
  let provider: NodeTracerProvider;
  let inst: PortkeyInstrumentation;
  let client: any;
  let frames: any[];
  let wire: WireRequest[];
  let requests: unknown[];
  let savedHide: string | undefined;

  beforeAll(async () => {
    server = createServer((req, res) => {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        if (req.url === '/v1/traces') {
          wire.push(JSON.parse(body));
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end('{}');
        } else {
          requests.push(JSON.parse(body));
          res.writeHead(200, { 'content-type': 'text/event-stream' });
          res.end(frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join('') + 'data: [DONE]\n\n');
        }
      });
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/traces`,
    }))] });
  });
  beforeEach(() => {
    frames = [...textEvents(), terminal('failed', [])];
    wire = [];
    requests = [];
    savedHide = process.env.FI_HIDE_ERROR_DETAILS;
    delete process.env.FI_HIDE_ERROR_DETAILS;
    client = new sdk.Portkey({ apiKey: credential, maxRetries: 0, baseURL: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1` });
  });
  afterEach(() => {
    inst?.disable();
    if (savedHide === undefined) delete process.env.FI_HIDE_ERROR_DETAILS;
    else process.env.FI_HIDE_ERROR_DETAILS = savedHide;
  });
  afterAll(async () => {
    await provider.shutdown();
    await new Promise<void>(resolve => server.close(() => resolve()));
  });
  const setup = (config?: TraceConfigOptions) => {
    inst = new PortkeyInstrumentation({ traceConfig: config });
    inst.setTracerProvider(provider);
    inst.manuallyInstrument(client);
  };
  const request = () => client.responses.create({ model: 'requested-model', input: 'hello', stream: true });
  const consume = async () => {
    const yielded = [];
    for await (const event of await request()) yielded.push(event);
    return yielded;
  };
  const onlySpan = async () => {
    await provider.forceFlush();
    const spans = wire.flatMap(request => request.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    expect(spans).toHaveLength(1);
    expect(JSON.stringify(wire)).not.toContain(credential);
    return spans[0];
  };
  const unknownUsage = (span: WireSpan) => {
    for (const key of [S.LLM_TOKEN_COUNT_PROMPT, S.LLM_TOKEN_COUNT_COMPLETION, S.LLM_TOKEN_COUNT_TOTAL,
      S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ, S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]) {
      expect(span.attributes.find(item => item.key === key)).toBeUndefined();
    }
  };

  it.each(['failed', 'incomplete'].flatMap(status => [false, true].flatMap(missing => policies.map(policy => ({ status, missing, ...policy })))))(
    '$status with missing output=$missing / $name retains observed text and terminal metadata', async mode => {
      frames = [...textEvents(), terminal(mode.status, mode.missing ? undefined : [])];
      // The instrumented SDK must yield the same events as the uninstrumented SDK.
      expect(await consume()).toEqual(frames);
      expect(wire).toHaveLength(0);
      if (mode.env) process.env.FI_HIDE_ERROR_DETAILS = mode.env;
      setup(mode.config);
      expect(await consume()).toEqual(frames);
      expect(requests).toHaveLength(2);
      expect(requests[1]).toEqual(requests[0]);
      const span = await onlySpan();
      const content = mode.pii ? partial.replace('person@example.com', '<EMAIL_ADDRESS>') : partial;
      expect(attribute(span, S.OUTPUT_VALUE)).toBe(content);
      expect(JSON.parse(attribute(span, S.LLM_OUTPUT_MESSAGES)!)).toEqual([{ role: 'assistant', content }]);
      const raw = JSON.parse(attribute(span, S.RAW_OUTPUT)!);
      expect(raw).toMatchObject({ id: 'resp-partial', model: 'returned-model', status: mode.status, usage: null });
      expect(raw.output).toEqual([{ type: 'message', role: 'assistant', content }]);
      expect(span.events ?? []).toHaveLength(0);
      expect(span.status.message).toBeUndefined();
      if (mode.status === 'failed') {
        expect(span.status.code).toBe(2);
        expect(raw.error).toEqual(mode.hideError ? { code: 'server_error' } : { code: 'server_error', message: errorDetail, details: errorDetail });
      } else {
        expect(raw.incomplete_details).toEqual({ reason: 'max_output_tokens' });
        expect(span.status.code).not.toBe(1); // Partial output is not successful completion.
      }
      if (mode.hideError) expect(JSON.stringify(wire)).not.toContain(errorDetail);
      if (mode.pii) expect(JSON.stringify(wire)).not.toContain('person@example.com');
      unknownUsage(span); // No fabricated zero counts or inferred billing from text.
    },
  );

  it.each(['hideOutputs', 'hideOutputMessages', 'hideOutputText'] as const)(
    '%s prevents partial-output fallback from bypassing content hiding', async option => {
      setup({ [option]: true, hideErrorDetails: true });
      expect(await consume()).toEqual(frames);
      const span = await onlySpan();
      expect(span.status.code).toBe(2);
      expect(JSON.stringify(wire)).not.toMatch(/PARTIAL-CONTENT-CANARY|person@example.com|ERROR-DETAIL-CANARY/);
      expect(attribute(span, S.RAW_OUTPUT)).toBeUndefined();
      unknownUsage(span);
    },
  );

  it.each(['failed', 'incomplete', 'completed'])(
    '%s prefers canonical nonempty output without duplicating earlier deltas or usage', async status => {
      const output = [{ type: 'function_call', call_id: 'call-final', name: 'lookup', arguments: '{"done":true}' }];
      const event = { ...terminal(status, output), response: { ...terminal(status, output).response,
        usage: { input_tokens: 4, output_tokens: 2, total_tokens: 6, input_tokens_details: { cached_tokens: 0 } } } };
      frames = [...textEvents(), event];
      setup({ hideErrorDetails: true });
      expect(await consume()).toEqual(frames);
      const span = await onlySpan();
      expect(JSON.parse(attribute(span, S.RAW_OUTPUT)!)).toMatchObject({ output, status, usage: event.response.usage });
      expect(JSON.parse(attribute(span, S.LLM_OUTPUT_MESSAGES)!)).toEqual([{ role: 'assistant', tool_calls: [
        { id: 'call-final', type: 'function', function: { name: 'lookup', arguments: '{"done":true}' } },
      ] }]);
      expect(Number(span.attributes.find(item => item.key === S.LLM_TOKEN_COUNT_TOTAL)?.value.intValue)).toBe(6);
      expect(Number(span.attributes.find(item => item.key === S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ)?.value.intValue)).toBe(0);
      expect(JSON.stringify(wire)).not.toContain('PARTIAL-CONTENT-CANARY');
    },
  );

  it.each([false, true])('iterator return after terminal=%s ends once with only observed partial output', async afterTerminal => {
    setup({ hideErrorDetails: true });
    const iterator = (await request())[Symbol.asyncIterator]();
    expect((await iterator.next()).value).toEqual(frames[0]);
    expect((await iterator.next()).value).toEqual(frames[1]);
    if (afterTerminal) expect((await iterator.next()).value).toEqual(frames[2]);
    expect(wire).toHaveLength(0);
    expect((await iterator.return()).done).toBe(true);
    expect((await iterator.next()).done).toBe(true);
    const span = await onlySpan();
    expect(attribute(span, S.OUTPUT_VALUE)).toBe(partial);
    if (afterTerminal) {
      expect(span.status.code).toBe(2);
      expect(JSON.parse(attribute(span, S.RAW_OUTPUT)!)).toMatchObject({ status: 'failed', error: { code: 'server_error' } });
    }
    unknownUsage(span);
  });

  it('retains observed partial tool arguments on failure without completing them', async () => {
    frames = [
      { type: 'response.output_item.added', output_index: 1, item: { type: 'function_call', call_id: 'partial-tool', name: 'lookup' } },
      { type: 'response.function_call_arguments.delta', output_index: 1, delta: '{"unfinished":' },
      terminal('failed', []),
    ];
    setup({ hideErrorDetails: true });
    expect(await consume()).toEqual(frames);
    const span = await onlySpan();
    expect(span.status.code).toBe(2);
    expect(JSON.parse(attribute(span, S.LLM_OUTPUT_MESSAGES)!)).toEqual([{ role: 'assistant', tool_calls: [
      { id: 'partial-tool', type: 'function', function: { name: 'lookup', arguments: '{"unfinished":' } },
    ] }]);
    expect(JSON.parse(attribute(span, S.RAW_OUTPUT)!)).toMatchObject({ status: 'failed', error: { code: 'server_error' }, usage: null });
    unknownUsage(span);
  });

  it('does not fabricate partial output when failure precedes any delta', async () => {
    frames = [terminal('failed', [])];
    setup({ hideErrorDetails: true });
    expect(await consume()).toEqual(frames);
    const span = await onlySpan();
    expect(span.status.code).toBe(2);
    expect(JSON.parse(attribute(span, S.LLM_OUTPUT_MESSAGES)!)).toEqual([]);
    expect(JSON.parse(attribute(span, S.RAW_OUTPUT)!)).toMatchObject({ output: [], status: 'failed', usage: null });
    unknownUsage(span);
  });
});
