'use strict';
const http = require('node:http');
const https = require('node:https');
const fs = require('node:fs');
const { context, trace, propagation, ROOT_CONTEXT, SpanKind, createTraceState, baggageEntryMetadataFromString } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');
const auto = require(process.env.F4_AUTO);
const host = globalThis.__f4;
const body = Buffer.from([0, 1, 127, 128, 255, 10]);
const rows = [];
const lookup = host.lookup;
const authorityOptions = process.env.F4_CASE === 'authority' ? {
  ...(process.env.F4_AGENT_PORT ? { port: Number(process.env.F4_AGENT_PORT) } : {}),
  ...(process.env.F4_DEFAULT_PORT ? { defaultPort: Number(process.env.F4_DEFAULT_PORT) } : {}),
  ...(process.env.F4_AGENT_HOST ? { host: process.env.F4_AGENT_HOST } : {}),
} : {};
const agent = new http.Agent({ keepAlive: true, lookup, ...authorityOptions });
const tlsAgent = process.env.F4_CA ? new https.Agent({ keepAlive: true, lookup, ca: fs.readFileSync(process.env.F4_CA), ...authorityOptions }) : undefined;
let failureStage = 'startup', failureTransport = 'unknown', failureVariant = 'unknown';
const transports = new Set(['http', 'https']);
const variantNames = new Set(['', 'string', 'url', 'options', 'raw', 'late', 'flush', 'write', 'get', 'cached']);
// Error messages/stacks may contain URLs/headers. Emit only allowlisted codes.
function failureCode(error) {
  const code = error && Object.getOwnPropertyDescriptor(error, 'code')?.value;
  return ['F4_HTTP_STATUS', 'ERR_HTTP_HEADERS_SENT', 'ERR_INVALID_ARG_TYPE', 'ERR_TLS_CERT_ALTNAME_INVALID', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN'].includes(code) ? code : error instanceof SyntaxError ? 'JSON_PARSE' : error instanceof TypeError ? 'TYPE_ERROR' : 'OTHER';
}
function native(url, id, variant, headers, useHttps = false) {
  return new Promise((resolve, reject) => {
    const transport = useHttps ? https : http;
    const request = globalThis.__f4NamedRequest || (variant === 'cached' && !useHttps ? host.cachedRequest : transport.request);
    const options = { method: variant === 'get' ? 'GET' : 'POST', headers, agent: useHttps ? tlsAgent : agent,
      ...(process.env.F4_CASE === 'authority' && process.env.F4_DEFAULT_PORT ? { defaultPort: Number(process.env.F4_DEFAULT_PORT) } : {}) };
    const done = response => {
      if (response.statusCode !== 200) {
        response.resume(); const error = new Error('F4 unexpected response'); error.code = 'F4_HTTP_STATUS'; reject(error); return;
      }
      const chunks = []; response.on('data', chunk => chunks.push(chunk)); response.on('error', reject);
      response.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString())); } catch (error) { reject(error); } });
    };
    let req;
    if (process.env.F4_CASE === 'authority') {
      const u = new URL(url);
      req = request({ ...options, protocol: u.protocol, hostname: u.hostname, port: u.port,
        path: process.env.F4_LITERAL_PATH || u.pathname + u.search }, done);
    }
    else if (variant === 'get') { req = (useHttps ? transport.get : host.cachedGet)(url, options, done); }
    else if (variant === 'options') { const u = new URL(url); req = request({ ...options, protocol: u.protocol, hostname: u.hostname, port: u.port, path: u.pathname + u.search }, done); }
    else if (variant === 'string') req = request(url, options, done);
    else req = request(new URL(url), options, done);
    req.on('error', reject);
    if (variant !== 'get') {
      if (variant === 'late') req.setHeader('x-f4-late', 'kept');
      if (variant === 'flush') req.flushHeaders();
      if (variant === 'write') { req.write(body.subarray(0, 2)); req.end(body.subarray(2)); }
      else req.end(body);
    }
  });
}
async function send(id, transport, variant = '', ownership = '') {
  failureStage = 'send'; failureTransport = transports.has(transport) ? transport : 'unknown'; failureVariant = variantNames.has(variant) ? variant : 'unknown';
  const target = new URL(process.env.F4_URL); target.pathname = '/ordinary'; target.searchParams.set('id', id);
  if (process.env.F4_HOST) target.hostname = process.env.F4_HOST;
  let headers = { 'x-f4-id': id, 'x-f4-original': 'preserved' };
  if (ownership === 'empty') headers.TrAcEpArEnT = '';
  if (ownership === 'existing') headers.TrAcEpArEnT = '00-11111111111111111111111111111111-2222222222222222-01';
  if (ownership === 'other') { headers.baggage = 'app=owned'; headers.tracestate = 'app=owned'; }
  // Raw arrays are the COMPLETE header block: Node skips generated Host and
  // serializes it in the constructor, before end(body) can set a known length.
  if (variant === 'raw') headers = ['Host', target.host, 'Content-Length', String(body.length), 'x-f4-id', id, 'x-duplicate', 'one', 'x-duplicate', 'two'];
  if (process.env.F4_CASE === 'authority') {
    if (process.env.F4_HOST_HEADER) headers.Host = process.env.F4_HOST_HEADER;
    if (process.env.F4_EARLY_HEADERS === 'raw') headers = ['Host', process.env.F4_HOST_HEADER || target.host, 'Content-Length', String(body.length), 'x-f4-original', 'preserved'];
    if (process.env.F4_EARLY_HEADERS === 'expect') headers.Expect = '100-continue';
  }
  const before = JSON.stringify(headers);
  let span, ctx = ROOT_CONTEXT;
  if (ownership === 'remote') ctx = trace.setSpanContext(ctx, { traceId: '13579bdf13579bdf13579bdf13579bdf', spanId: '2468ace02468ace0', traceFlags: 0, isRemote: true });
  else if (ownership === 'opaque') ctx = trace.setSpanContext(ctx, { traceId: '13579bdf13579bdf13579bdf13579bdf', spanId: '2468ace02468ace0', traceFlags: 1 });
  else if (!['root', 'baggage-only'].includes(ownership)) {
    span = host.provider.getTracer('f4-host').startSpan(id, { kind: ownership === 'client' ? SpanKind.CLIENT : SpanKind.INTERNAL });
    ctx = trace.setSpan(ctx, span);
  }
  if (ownership === 'baggage' || ownership === 'baggage-only') {
    ctx = propagation.setBaggage(ctx, propagation.createBaggage({ region: { value: 'west' }, authorization: { value: 'synthetic-private-baggage' }, link: { value: 'https://example.invalid/?token=synthetic-signed-value' } }));
    const sc = trace.getSpanContext(ctx);
    // Explicit remote context for tracestate without changing the real parent's identity.
    if (sc) ctx = trace.setSpanContext(ctx, { ...sc, isRemote: true, traceState: createTraceState('region=west') });
  }
  if (process.env.F4_METADATA === 'alias') {
    ctx = propagation.setBaggage(ctx, propagation.createBaggage({ region: { value: 'west' }, probe: { value: 'safe', metadata: baggageEntryMetadataFromString('note=čĊx-f4-probe:yes') } }));
  }
  if (ownership === 'suppressed') ctx = suppressTracing(ctx);
  const sc = trace.getSpanContext(ctx);
  const expected = sc ? `00-${sc.traceId}-${sc.spanId}-${(sc.traceFlags & 1) ? '01' : '00'}` : undefined;
  let response;
  try {
    response = await context.with(ctx, async () => {
      if (!transports.has(transport)) throw new Error('F4 unsupported fixture transport');
      return native(target.href, id, variant, headers, transport === 'https');
    });
  } finally { span?.end(); }
  rows.push({ id, expected, response, headersUnchanged: before === JSON.stringify(headers), transport, variant, ownership });
}
// Application-driven redirects: the agent itself never follows or replays one.
async function redirects() {
  const start = new URL(process.env.F4_URL), middle = new URL(process.env.F4_REDIRECT_URL || start.href), last = new URL(start.href);
  const form = process.env.F4_REDIRECT_FORM || 'absolute';
  const prefix = form.startsWith('relative-') ? '/nested' : '';
  start.pathname = prefix + '/redirect-start'; middle.pathname = prefix + '/redirect-middle'; last.pathname = prefix + '/redirect-last';
  if (form === 'relative-parent') middle.pathname = '/redirect-middle';
  if (form === 'relative-query') middle.pathname = start.pathname;
  start.searchParams.set('id', 'redirect-start'); middle.searchParams.set('id', 'redirect-middle'); last.searchParams.set('id', 'redirect-last');
  middle.searchParams.set('next', last.href);
  let location = middle.href;
  if (form === 'scheme-bare') location = middle.href.replace('://', ':');
  else if (form === 'scheme-slash') location = middle.href.replace('://', ':/');
  else if (form === 'relative-path') location = './redirect-middle' + middle.search;
  else if (form === 'relative-parent') location = '../redirect-middle' + middle.search;
  else if (form === 'relative-query') location = middle.search;
  else if (form === 'network-path') location = '//' + middle.host + middle.pathname + middle.search;
  else if (form === 'uppercase') location = middle.protocol.toUpperCase() + '//' + middle.host.toUpperCase() + middle.pathname + middle.search;
  start.searchParams.set('next', location);
  const directString = form === 'scheme-bare' || form === 'scheme-slash';
  const resolveHop = (target, base) => new URL(target, directString ? undefined : base);
  const span = host.provider.getTracer('f4-host').startSpan('redirect-chain', { kind: SpanKind.INTERNAL });
  let ctx = trace.setSpan(ROOT_CONTEXT, span);
  const sc = span.spanContext();
  ctx = trace.setSpanContext(ctx, { ...sc, isRemote: true, traceState: createTraceState('region=west') });
  ctx = propagation.setBaggage(ctx, propagation.createBaggage({ region: { value: 'west' } }));
  const expected = `00-${sc.traceId}-${sc.spanId}-${(sc.traceFlags & 1) ? '01' : '00'}`;
  const sendHop = (target, index, after, base) => new Promise((resolve, reject) => {
    const u = resolveHop(target, base), secure = u.protocol === 'https:';
    // The ambiguity regression passes the Location bytes directly to native
    // request(string). Do not pre-resolve those bytes against the previous URL.
    const input = directString && index === 1 ? target : u;
    let headers = { 'x-f4-original': 'preserved' };
    if (process.env.F4_REDIRECT_OWNED === '1' && index > 0) { headers.traceparent = '00-11111111111111111111111111111111-2222222222222222-01'; headers.tracestate = 'app=owned'; headers.baggage = 'app=owned'; }
    if (process.env.F4_EARLY_HEADERS === 'raw') headers = ['Host', u.host, 'Content-Length', String(body.length), ...Object.entries(headers).flat()];
    else if (process.env.F4_EARLY_HEADERS === 'expect') headers.Expect = '100-continue';
    const before = JSON.stringify(headers);
    const req = (secure ? https : http).request(input, { agent: secure ? tlsAgent : agent, method: 'POST', headers }, response => {
      const chunks = []; response.on('data', chunk => chunks.push(chunk)); response.on('error', reject);
      // A new span made in the response listener must inherit the closed chain,
      // even if the application drops the old baggage object.
      let childContext, childSpan;
      if (process.env.F4_REDIRECT_FLOW === 'child' && response.headers.location) {
        childSpan = host.provider.getTracer('f4-host').startSpan('redirect-child', { kind: SpanKind.INTERNAL });
        childContext = propagation.deleteBaggage(trace.setSpan(context.active(), childSpan));
      }
      response.on('end', () => {
        try {
          const row = JSON.parse(Buffer.concat(chunks).toString());
          rows.push({ id: u.searchParams.get('id'), expected, response: row, headersUnchanged: before === JSON.stringify(headers), requestInputType: typeof input === 'string' ? 'string' : 'URL' });
          if (![200, 307].includes(response.statusCode)) throw new Error('F4 redirect status');
          const next = response.headers.location;
          if (after && next) {
            const follow = () => after(next, index + 1, u.href);
            Promise.resolve(childContext ? context.with(childContext, follow) : follow()).then(value => { childSpan?.end(); resolve(value); }, error => { childSpan?.end(); reject(error); });
          } else resolve(next);
        } catch (error) { childSpan?.end(); reject(error); }
      });
    });
    req.on('error', reject); req.end(body);
  });
  try {
    await context.with(ctx, async () => {
      if (process.env.F4_REDIRECT_FLOW === 'await' || process.env.F4_REDIRECT_FLOW === 'precreated') {
        // Allocate before observing Location, then detach baggage. The observed
        // parent/child relationship must still close when the response arrives.
        const childSpan = process.env.F4_REDIRECT_FLOW === 'precreated' ? host.provider.getTracer('f4-host').startSpan('precreated-child', { kind: SpanKind.INTERNAL }) : undefined;
        const childContext = childSpan ? propagation.deleteBaggage(trace.setSpan(ROOT_CONTEXT, childSpan)) : undefined;
        try {
          let target = start.href, base;
          for (let i = 0; target && i < 3; i++) {
            const current = resolveHop(target, base).href;
            target = i > 0 && childContext ? await context.with(childContext, () => sendHop(target, i, undefined, base)) : await sendHop(target, i, undefined, base);
            base = current;
          }
        } finally { childSpan?.end(); }
      } else {
        const follow = (target, index, base) => {
          if (index > 2) throw new Error('F4 redirect count');
          return sendHop(target, index, follow, base);
        };
        await follow(start.href, 0);
      }
    });
  } finally { span.end(); }
  // Fresh unrelated context remains eligible after the chain is fenced.
  await send('unrelated-after-redirect', start.protocol === 'https:' ? 'https' : 'http');
}
async function run() {
  if (process.env.F4_MANUAL === '1') auto.init({ provider: host.provider, wireCapture: false, instrumentations: ['wire'] });
  const pending = auto.report();
  const transport = process.env.F4_TRANSPORT || 'http';
  const variants = ['string', 'url', 'options', 'raw', 'late', 'flush', 'write', 'get', 'cached'];
  if (process.env.F4_CASE === 'redirect') await redirects();
  else if (process.env.F4_CASE === 'authority') await send('authority', transport, '', process.env.F4_OWNERSHIP === 'existing' ? 'existing' : 'baggage');
  else if (process.env.F4_CASE === 'ownership') {
    for (const kind of ['eligible', 'existing', 'empty', 'other', 'client', 'suppressed', 'root', 'opaque', 'remote', 'baggage', 'baggage-only']) await send(kind, transport, '', kind);
  } else if (process.env.F4_CASE === 'concurrent') await Promise.all(Array.from({ length: 8 }, (_, i) => send(`parallel-${i}`, transport)));
  else if (process.env.F4_HOST) await send('host-policy', transport, '', process.env.F4_OWNERSHIP === 'existing' ? 'existing' : '');
  else for (const variant of variants) await send(variant, transport, variant);
  failureStage = 'report';
  const beforeShutdown = auto.report();
  const globalsPreserved = (trace.getTracerProvider().getDelegate?.() || trace.getTracerProvider()) === host.provider && propagation._getGlobalPropagator() === host.custom && context._getContextManager() === host.manager;
  failureStage = 'shutdown';
  await auto.shutdown(); await auto.shutdown();
  if (process.env.F4_CASE === 'lifecycle') await send('after-shutdown', transport);
  failureStage = 'cleanup';
  agent.destroy(); tlsAgent?.destroy();
  const telemetry = JSON.stringify({ pending, beforeShutdown, spans: host.spans.map(s => ({ attributes: s.attributes, events: s.events })) });
  const telemetryClean = !/synthetic-private|synthetic-signed|region=west|x-f4-original|x-private-custom/.test(telemetry) && rows.every(row => !row.expected || !telemetry.includes(row.expected));
  process.stdout.write(JSON.stringify({ rows, pending, beforeShutdown, globalsPreserved, telemetryClean, spanKinds: host.spans.map(s => s.kind) }));
}
run().catch(error => {
  const cause = error && Object.getOwnPropertyDescriptor(error, 'cause')?.value;
  process.stderr.write(`F4 fixture failed ${JSON.stringify({ stage: failureStage, transport: failureTransport, variant: failureVariant, code: failureCode(error), cause: failureCode(cause), node: /^v[0-9.]+$/.test(process.version) ? process.version : 'unknown' })}\n`);
  process.exitCode = 1; agent.destroy(); tlsAgent?.destroy();
});
