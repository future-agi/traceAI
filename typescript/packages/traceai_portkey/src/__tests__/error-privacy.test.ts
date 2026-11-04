import { createServer, Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { TraceConfigOptions } from '@traceai/fi-core';
import { SemanticConventions as S } from '@traceai/fi-semantic-conventions';
import { PortkeyInstrumentation } from '../instrumentation';

const email = 'person@example.com';
const message = `Synthetic failure for ${email}`;
const credential = 'SYNTHETIC-KEY';
const sdks = [
  ['1.0.0', require('portkey-floor')], ['1.10.4', require('portkey-v1')],
  ['2.0.2', require('portkey-v2')], ['3.1.0', require('portkey-ai')],
] as const;
const modes: { name: string; env?: string; config?: TraceConfigOptions; redacted: boolean; hidden?: boolean }[] = [
  { name: 'environment', env: 'true', redacted: true },
  { name: 'explicit option', config: { piiRedaction: true }, redacted: true },
  { name: 'explicit opt-out', env: 'true', config: { piiRedaction: false }, redacted: false },
  { name: 'default', redacted: false },
  { name: 'hide error details takes precedence', env: 'true', config: { hideErrorDetails: true }, redacted: true, hidden: true },
];

type WireAttributes = { key: string; value: { stringValue?: string } }[];
interface WireSpan {
  status: { code: number; message?: string };
  attributes: WireAttributes;
  events: { name: string; attributes: WireAttributes }[];
}
interface WireRequest {
  resourceSpans: { scopeSpans: { spans: WireSpan[] }[] }[];
}
const attribute = (attributes: WireAttributes, key: string) =>
  attributes.find(item => item.key === key)?.value.stringValue;

// Real SDK HTTP errors and real OTLP exports, both served over loopback. This
// catches shared-core regressions that input/output attribute masking misses.
describe.each(sdks)('exception privacy / portkey-ai %s', (_version, sdk) => {
  let server: Server;
  let provider: NodeTracerProvider;
  let inst: PortkeyInstrumentation;
  let baseURL: string;
  let received: unknown[];
  let wire: WireRequest[];
  let savedPii: string | undefined;
  let savedHide: string | undefined;

  beforeAll(async () => {
    server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const body = JSON.parse(Buffer.concat(chunks).toString());
        if (req.url === '/v1/traces') {
          wire.push(body);
          res.writeHead(200, { 'content-type': 'application/json' });
          res.end('{}');
        } else {
          received.push(body);
          res.writeHead(400, { 'content-type': 'application/json', 'x-request-id': 'synthetic-request' });
          res.end(JSON.stringify({ error: { message } }));
        }
      });
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
    provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `${baseURL}/traces`,
    }))] });
  });
  beforeEach(() => {
    received = [];
    wire = [];
    savedPii = process.env.FI_PII_REDACTION;
    savedHide = process.env.FI_HIDE_ERROR_DETAILS;
    delete process.env.FI_PII_REDACTION;
    delete process.env.FI_HIDE_ERROR_DETAILS;
  });
  afterEach(() => {
    inst?.disable();
    if (savedPii === undefined) delete process.env.FI_PII_REDACTION;
    else process.env.FI_PII_REDACTION = savedPii;
    if (savedHide === undefined) delete process.env.FI_HIDE_ERROR_DETAILS;
    else process.env.FI_HIDE_ERROR_DETAILS = savedHide;
  });
  afterAll(async () => {
    await provider.shutdown();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });

  it.each(modes)('$name preserves the caller error while applying the export policy', async mode => {
    if (mode.env !== undefined) process.env.FI_PII_REDACTION = mode.env;
    inst = new PortkeyInstrumentation({ traceConfig: mode.config });
    inst.setTracerProvider(provider);
    const client = new sdk.Portkey({ apiKey: credential, baseURL, maxRetries: 0 });
    inst.manuallyInstrument(client);
    const input = { model: 'synthetic-model', messages: [{ role: 'user', content: email }] };
    const pending = client.chat.completions.create(input);
    const error = await pending.catch((value: unknown) => value);
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toContain(message);
    expect(error.stack).toContain(message);
    await expect(pending).rejects.toBe(error);
    expect(received).toEqual([input]);

    await provider.forceFlush();
    const spans = wire.flatMap(request => request.resourceSpans.flatMap(resource =>
      resource.scopeSpans.flatMap(scope => scope.spans)));
    expect(spans).toHaveLength(1);
    const [span] = spans;
    expect(span.status.code).toBe(2);
    expect(span.events).toHaveLength(1);
    expect(span.events[0].name).toBe('exception');
    expect(attribute(span.attributes, 'error.type')).toEqual(expect.any(String));
    expect(attribute(span.events[0].attributes, 'exception.type')).toEqual(expect.any(String));
    const details = {
      status: span.status.message,
      message: attribute(span.events[0].attributes, 'exception.message'),
      stack: attribute(span.events[0].attributes, 'exception.stacktrace'),
    };
    expect(details).toEqual(mode.hidden
      ? { status: undefined, message: undefined, stack: undefined }
      : {
        status: expect.stringContaining(mode.redacted ? '<EMAIL_ADDRESS>' : message),
        message: expect.stringContaining(mode.redacted ? '<EMAIL_ADDRESS>' : message),
        stack: expect.stringContaining(mode.redacted ? '<EMAIL_ADDRESS>' : message),
      });
    expect(attribute(span.attributes, S.INPUT_VALUE)).toContain(mode.redacted ? '<EMAIL_ADDRESS>' : email);
    const serialized = JSON.stringify(wire);
    if (mode.redacted) expect(serialized).not.toContain(email);
    else expect(serialized).toContain(email);
    expect(serialized).not.toContain(credential);
  });
});
