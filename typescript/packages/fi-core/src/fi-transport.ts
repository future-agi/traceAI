/**
 * fi-core's own HTTP transport for key-carrying sends: undici's Agent and ProxyAgent, driven through undici's request API.
 *
 * undici's package index loads `lib/global.js`, which reads the process-wide slot `Symbol.for("undici.globalDispatcher.1")` (running any getter the
 * application put there) and writes undici's own Agent into it when it is empty, so that the application's own fetch would be dispatched by
 * this copy of undici instead of Node's. The modules imported below never load it: `lib/dispatcher/agent.js`, `lib/dispatcher/proxy-agent.js`
 * and `lib/api/index.js`, imported by path. Nothing in fi-core imports "undici" itself as a value, so loading fi-core neither reads nor writes
 * the slot (fiSendGuardR9f.test.ts and the @traceai/auto undiciSlot.test.ts measure that on the built package and on the agent bundle).
 * undici's own `fetch` is not used either: it takes the global dispatcher when none is given.
 */
import { STATUS_CODES } from "node:http";
import { Readable } from "node:stream";
import type { Dispatcher } from "undici";
// undici's package has no `exports` map, so these paths resolve; they ship no type declarations.
// @ts-ignore untyped deep import
import AgentClass from "undici/lib/dispatcher/agent.js";
// @ts-ignore untyped deep import
import ProxyAgentClass from "undici/lib/dispatcher/proxy-agent.js";
// @ts-ignore untyped deep import
import api from "undici/lib/api/index.js";
// @ts-ignore untyped deep import
import DispatcherClass from "undici/lib/dispatcher/dispatcher.js";

// What undici's index does before anything else, for this private copy of its classes: a proxy agent opens its tunnel with `client.connect`.
Object.assign((DispatcherClass as { prototype: object }).prototype, api);

type Entry = [string, string];

/** The entries of the headers a fetch will send, whatever shape they have (a Headers object, an array of pairs, a record). */
export function headerEntries(headers: unknown): Entry[] {
  const entries: Entry[] = [];
  if (headers === undefined || headers === null) return entries;
  try {
    if (typeof (headers as Headers).forEach === "function" && typeof (headers as Headers).get === "function") { (headers as Headers).forEach((value, name) => entries.push([name, String(value)])); return entries; }
    if (Array.isArray(headers)) { for (const pair of headers) if (Array.isArray(pair) && pair.length >= 2) entries.push([String(pair[0]), String(pair[1])]); return entries; }
    for (const [name, value] of Object.entries(headers as Record<string, unknown>)) {
      if (Array.isArray(value)) for (const item of value) entries.push([name, String(item)]);
      else if (value !== undefined) entries.push([name, String(value)]);
    }
  } catch { /* an unreadable container adds nothing */ }
  return entries;
}

/** A new direct agent: TLS verification always on whatever NODE_TLS_REJECT_UNAUTHORIZED says. */
export function newDirectAgent(): Dispatcher {
  return new AgentClass({ connect: { rejectUnauthorized: true } }) as Dispatcher;
}
/** A new CONNECT-only proxy agent: the destination's certificate and the proxy's own (for an https proxy) are always verified. */
export function newProxyAgent(uri: string): Dispatcher {
  return new ProxyAgentClass({ uri, requestTls: { rejectUnauthorized: true }, proxyTls: { rejectUnauthorized: true } }) as Dispatcher;
}

export interface OwnFetchInit {
  method?: string;
  headers?: unknown;
  body?: string | Uint8Array;
  signal?: AbortSignal;
  /** Always the transport's own dispatcher. */
  dispatcher: Dispatcher;
  /** A redirect answer is returned as it is, never followed. */
  redirect?: "manual";
}

/** The part of a response the callers read: status line, headers by name, the body as bytes, text or JSON, and a way to drop the body. */
export interface OwnResponse {
  readonly status: number;
  readonly statusText: string;
  readonly ok: boolean;
  readonly headers: { get(name: string): string | null };
  readonly body: { cancel(): Promise<void> } | null;
  arrayBuffer(): Promise<ArrayBuffer>;
  text(): Promise<string>;
  json(): Promise<unknown>;
}

/**
 * A response of a plain object, not a global `Response` or `Headers`: Node loads its own undici lazily on first use of those, and that load
 * reads the process-wide dispatcher slot (running a getter the application put there) or fills it when it is empty.
 */
/** Drops a body: undici's readable emits an abort error when destroyed, which nothing listens to. */
function dropBody(body: { destroy(): unknown; on?: (event: string, listener: () => void) => unknown }): void {
  try { body.on?.("error", () => undefined); } catch { /* nothing to listen on */ }
  body.destroy();
}

function ownResponse(statusCode: number, headers: Map<string, string>, body: (Readable & { destroy(): unknown }) | null): OwnResponse {
  let read: Promise<Buffer> | undefined;
  const bytes = (): Promise<Buffer> => {
    if (read === undefined) {
      read = (async () => {
        if (body === null) return Buffer.alloc(0);
        const chunks: Buffer[] = [];
        for await (const chunk of body) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
        return Buffer.concat(chunks);
      })();
    }
    return read;
  };
  return {
    status: statusCode,
    statusText: STATUS_CODES[statusCode] ?? "",
    ok: statusCode >= 200 && statusCode < 300,
    headers: { get: (name: string) => headers.get(name.toLowerCase()) ?? null },
    body: body === null ? null : { cancel: async () => { dropBody(body); } },
    arrayBuffer: async () => { const b = await bytes(); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer; },
    text: async () => (await bytes()).toString("utf8"),
    json: async () => JSON.parse((await bytes()).toString("utf8")),
  };
}

const NO_BODY = new Set([101, 204, 205, 304]);

/**
 * A request through `init.dispatcher`. A redirect is returned as the 3xx answer it is; the body is not decompressed (no Accept-Encoding is sent).
 */
export async function ownFetch(url: string, init: OwnFetchInit): Promise<OwnResponse> {
  const target = new URL(url);
  const headers: string[] = [];
  for (const [name, value] of headerEntries(init.headers)) headers.push(name, value);
  const response = await (api.request as (this: Dispatcher, options: object) => Promise<{ statusCode: number; headers: Record<string, string | string[] | undefined>; body: Readable & { destroy(): unknown } }>).call(init.dispatcher, {
    origin: target.origin,
    path: `${target.pathname}${target.search}`,
    method: init.method ?? "GET",
    headers,
    body: init.body,
    signal: init.signal,
  });
  const out = new Map<string, string>();
  for (const [name, value] of Object.entries(response.headers)) {
    if (value !== undefined) out.set(name.toLowerCase(), Array.isArray(value) ? value.join(", ") : String(value));
  }
  const empty = NO_BODY.has(response.statusCode);
  if (empty) dropBody(response.body);
  return ownResponse(response.statusCode, out, empty ? null : response.body);
}
