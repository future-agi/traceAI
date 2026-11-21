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

const NO_BODY = new Set([101, 204, 205, 304]);

/**
 * A request through `init.dispatcher`, answered with a standard Response. A redirect is returned as the 3xx answer it is; the body is not
 * decompressed (no Accept-Encoding is sent).
 */
export async function ownFetch(url: string, init: OwnFetchInit): Promise<Response> {
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
  const out = new Headers();
  for (const [name, value] of Object.entries(response.headers)) {
    if (Array.isArray(value)) for (const item of value) out.append(name, item);
    else if (value !== undefined) out.append(name, String(value));
  }
  const empty = NO_BODY.has(response.statusCode);
  if (empty) response.body.destroy();
  return new Response(empty ? null : (Readable.toWeb(response.body) as unknown as ReadableStream), { status: response.statusCode, statusText: STATUS_CODES[response.statusCode] ?? "", headers: out });
}
