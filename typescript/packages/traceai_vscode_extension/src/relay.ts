import { createServer, type Server, type ServerResponse } from 'node:http';
import { gunzip } from 'node:zlib';
import { promisify } from 'node:util';
import { timingSafeEqual } from 'node:crypto';
import { postOtlp, type ResolvedConfig } from '@traceai/agent-core';
import { decode, encode, sanitize } from './otlp';

const unzip = promisify(gunzip);
export interface RelayState { received: number; exported: number; dropped: number; lastExport?: string; lastError?: string; }
export interface RelayOptions {
  port: number;
  token: string;
  config: () => Promise<ResolvedConfig>;
  projectDir?: string;
  maxBytes?: number;
  maxQueueBytes?: number;
  timeoutMs?: number;
  onState?: (state: RelayState, config?: ResolvedConfig) => void | Promise<void>;
}
export async function startRelay(options: RelayOptions) {
  if (!/^[a-f0-9]{64}$/.test(options.token)) throw new Error('INVALID_RELAY_TOKEN');
  const max = options.maxBytes ?? 1024 * 1024, queueLimit = options.maxQueueBytes ?? 4 * 1024 * 1024;
  const state: RelayState = { received: 0, exported: 0, dropped: 0 };
  const queue: { body: Buffer; protobuf: boolean }[] = [];
  let bytes = 0, draining = false, closing = false, ingress = 0;
  let outbound: AbortController | undefined;
  const notify = (config?: ResolvedConfig) => { void Promise.resolve(options.onState?.({ ...state }, config)).catch(() => {}); };
  const loss = (code: string, count = 1) => { state.dropped += count; state.lastError = code; notify(); };
  async function drain() {
    if (draining || closing) return;
    draining = true;
    try {
      while (queue.length && !closing) {
        const item = queue.shift()!;
        bytes -= item.body.length;
        try {
          // Read policy at export, not enqueue. Disable and privacy changes fence queued work.
          const config = await options.config();
          if (!config.enabled || !config.destinationApproved || !config.credentials) { loss('DISABLED'); continue; }
          const sanitized = sanitize(decode(item.body, item.protobuf), config, options.projectDir);
          const body = encode(sanitized.body, item.protobuf);
          if (body.length > max) { loss('TOO_LARGE', sanitized.count); continue; }
          if (config.revalidate && !await config.revalidate()) { loss('DISABLED', sanitized.count); continue; }
          if (closing) break;
          outbound = new AbortController();
          const result = await postOtlp({ endpoint: config.endpoint, projectName: config.projectName,
            credentials: config.credentials, body, protocol: item.protobuf ? 'http/protobuf' : 'http/json',
            timeoutMs: options.timeoutMs ?? 3000, signal: outbound.signal });
          outbound = undefined;
          if (result.code === 'ACCEPTED') {
            state.exported += sanitized.count;
            state.lastExport = result.acceptedAt ?? new Date().toISOString();
            state.lastError = undefined;
          } else { state.dropped += sanitized.count; state.lastError = result.code; }
          notify(config);
        } catch (error) {
          loss(error instanceof Error && error.message === 'WORKSPACE_ATTRIBUTION_REQUIRED' ? error.message : 'INVALID_OR_UNAVAILABLE');
        }
      }
    } finally { draining = false; }
  }
  const finish = (res: ServerResponse, code: number, protobuf = false) => {
    if (!res.headersSent) res.writeHead(code, { 'Content-Type': protobuf ? 'application/x-protobuf' : 'application/json', 'Cache-Control': 'no-store' });
    res.end(protobuf ? Buffer.alloc(0) : '{}');
  };
  const server: Server = createServer(async (req, res) => {
    // No CORS, no redirects, no wildcard host. Reject even empty Origin/Referer headers.
    const expected = `/${options.token}/v1/traces`, actual = req.url ?? '';
    const actualBytes = Buffer.from(actual), expectedBytes = Buffer.from(expected);
    const validPath = actualBytes.length === expectedBytes.length && timingSafeEqual(actualBytes, expectedBytes);
    const port = (server.address() as { port: number }).port;
    if (req.method !== 'POST' || !validPath || req.headers.host !== `127.0.0.1:${port}` ||
        req.headers.origin !== undefined || req.headers.referer !== undefined || req.headers['transfer-encoding'] && req.headers['content-length']) {
      finish(res, 403); req.resume(); return;
    }
    const type = (req.headers['content-type'] ?? '').split(';')[0].trim().toLowerCase();
    const protobuf = type === 'application/x-protobuf';
    if (!protobuf && type !== 'application/json') { finish(res, 415); req.resume(); return; }
    const encoding = req.headers['content-encoding'];
    if (encoding && encoding !== 'gzip' && encoding !== 'identity') { finish(res, 415); req.resume(); return; }
    if (closing || ingress >= 8 || queue.length >= 16 || bytes >= queueLimit) { loss('QUEUE_LIMIT'); finish(res, 200, protobuf); req.resume(); return; }
    if (Number(req.headers['content-length'] ?? 0) > max) { loss('TOO_LARGE'); finish(res, 413); req.resume(); return; }
    ingress++;
    try {
      const chunks: Buffer[] = []; let length = 0;
      for await (const chunk of req) {
        length += chunk.length;
        if (length > max) { loss('TOO_LARGE'); finish(res, 413); return; }
        chunks.push(Buffer.from(chunk));
      }
      let body: Buffer = Buffer.concat(chunks);
      if (encoding === 'gzip') body = await unzip(body, { maxOutputLength: max });
      if (body.length > max || bytes + body.length > queueLimit || queue.length >= 16) { loss('QUEUE_LIMIT'); finish(res, 200, protobuf); return; }
      state.received++;
      bytes += body.length;
      queue.push({ body, protobuf });
      finish(res, 200, protobuf); // Local receipt only; exporter failures never break Copilot.
      void drain();
    } catch { loss('INVALID_BODY'); finish(res, 400); }
    finally { ingress--; }
  });
  server.maxConnections = 16;
  server.requestTimeout = 5000;
  server.headersTimeout = 5000;
  server.timeout = 5000;
  server.keepAliveTimeout = 1000;
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port, '127.0.0.1', () => { server.removeListener('error', reject); resolve(); });
  });
  server.on('error', () => loss('LISTENER_ERROR'));
  return {
    port: (server.address() as { port: number }).port,
    state,
    fence() {
      outbound?.abort();
      if (queue.length) loss('DISABLED', queue.length);
      queue.length = 0; bytes = 0;
    },
    async stop() {
      closing = true; outbound?.abort(); queue.length = 0; bytes = 0;
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
    }
  };
}
