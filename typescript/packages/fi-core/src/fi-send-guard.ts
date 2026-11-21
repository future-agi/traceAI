/**
 * The send-level guard: Future AGI keys are judged at the SEND, on what is actually sent, by one function (guardSend), however
 * the request was assembled. The decisions in fi-request-policy.ts are the first layer, made on the inputs fi-core assembles; this
 * is the last one, made on the FINAL url and the FINAL headers or metadata, after every merge the transport performs
 * (OTEL_EXPORTER_OTLP_(TRACES_)HEADERS, the SDK's own headers, a caller's agent), and on the proxy and dispatcher in effect NOW.
 *
 * Three guarded entry points call guardSend and are the only places in the key-carrying modules that touch a transport
 * (fiSendGuardPin.test.ts reads the source and fails on any other):
 *   - guardedFetch: every key-carrying fetch (the deprecated HTTP exporter, the custom eval checks);
 *   - guardOtlpHttpExporter: the OTLP HTTP exporter's send, which sees the final url and headers of each request;
 *   - guardOtlpGrpcExporter: the OTLP gRPC exporter's channel and a client interceptor that sees the final metadata of each call.
 *
 * What the guard refuses, with a value-free FiKeyRefusal and nothing sent:
 *   - an FI key NAME (case-insensitive, percent-decoded) in the final set that the decision did not put there, or with another value;
 *   - an FI key VALUE (FI_API_KEY / FI_SECRET_KEY, or a key value the exporter's own options carry) under any other name;
 *   - a plaintext request that carries keys when the destination rules refuse it (Future AGI host, no opt-in, ambiguous host);
 *   - a plaintext request that carries keys through a proxy, loopback included: a dispatcher that may route it (a proxy agent, an
 *     environment-reading agent, an origin-bound dispatcher whose origin is not the target's, one that cannot be inspected),
 *     NODE_USE_ENV_PROXY, --use-env-proxy;
 *   - a redirect answer to a request that carried keys.
 * The sets of key values are only the Future AGI keys: no other vendor's credential, and not the secret registry.
 */
import * as grpc from "@grpc/grpc-js";
import { diag } from "@opentelemetry/api";
import { OTLPExporterError } from "@opentelemetry/otlp-exporter-base";
import { gzipSync } from "node:zlib";
import type { Dispatcher } from "undici";
import { headerEntries, newDirectAgent, newProxyAgent, ownFetch } from "./fi-transport";
import { type EndpointTransport } from "./fi-endpoint";
import { UNSUPPORTED_EXPORTER_LAYOUT } from "./otlp-header-isolation";
import { decideFiRequest, FiKeyRefusal, isRedirect, matchesNoProxy, undiciNoProxyExempts, type FiDecision } from "./fi-request-policy";

type Env = Readonly<Record<string, string | undefined>>;
type Entry = [string, string];
const KEY_NAME = /^x-(?:api|secret)-key$/i;

/** What the decision allowed to be on the wire: the key headers (names in lower case) and their values. */
export interface Intended { keys: ReadonlyMap<string, string>; values: readonly string[] }
/** The key headers and key values of a decision's outgoing set. */
export function intendedFrom(decision: Pick<FiDecision, "headers" | "metadata">): Intended {
  const keys = new Map<string, string>(), values = new Set<string>();
  const add = (name: string, value: unknown): void => {
    if (typeof value !== "string") return;
    if (KEY_NAME.test(name)) { keys.set(name.toLowerCase(), value); if (value.trim().length >= 8) values.add(value); }
  };
  for (const [name, value] of Object.entries(decision.headers ?? {})) add(name, value);
  if (decision.metadata) for (const name of Object.keys(decision.metadata.getMap())) for (const value of decision.metadata.get(name)) add(name, value);
  return { keys, values: [...values] };
}

/**
 * What may be logged about a failure: a refusal's own value-free message, otherwise the error's class and nothing else. An error from a
 * transport or from URL parsing can carry the whole URL, userinfo and query included, so its message is never logged.
 */
export function describeFailure(error: unknown): string {
  if (error instanceof FiKeyRefusal) return error.message;
  try { return error instanceof Error && typeof error.name === "string" ? `${error.name} (message withheld)` : "error (message withheld)"; } catch { return "error (message withheld)"; }
}
/** Shared by every copy of this package in the process: the first refusal to be reported explains itself, each later one names only its code. */
const REFUSAL_LATCH = Symbol.for("traceai.fi-core.refusal-message");
function firstRefusal(): boolean {
  try {
    if (Object.getOwnPropertyDescriptor(globalThis, REFUSAL_LATCH) !== undefined) return false;
    Object.defineProperty(globalThis, REFUSAL_LATCH, { value: true, enumerable: false, writable: false, configurable: false });
    return true;
  } catch { return false; }
}
/**
 * The end of a log line for a failed send: for a refusal, the explanatory message the first time in the process and only the code after that
 * (`not sent (FI_...)`); for any other error, its value-free description. Pre-setting the latch only hides the message, never a refusal.
 */
/**
 * What an export reports to its caller for a refusal: the refusal's code and nothing else (`not sent (FI_...)`). The explanation is given once per
 * process by the log line of the refusal (`failureSuffix`), so the error an export returns (which OpenTelemetry passes to its error handler for
 * every refused batch) and the status details of a gRPC call do not repeat it.
 */
export function refusalSummary(error: FiKeyRefusal): string { return `not sent (${error.code})`; }
function reportedRefusal(error: FiKeyRefusal): FiKeyRefusal {
  const reported = new FiKeyRefusal(error.code);
  Object.defineProperty(reported, "message", { value: refusalSummary(error), enumerable: false, writable: true, configurable: true });
  return reported;
}
export function failureSuffix(error: unknown): string {
  if (error instanceof FiKeyRefusal) return firstRefusal() ? `: ${error.message}` : ` (${error.code})`;
  return `: ${describeFailure(error)}`;
}

// --- the final set -----------------------------------------------------------------------------------------------

export { headerEntries };
/** The entries of a gRPC Metadata, every value of every name, binary ones skipped. */
export function metadataEntries(metadata: unknown): Entry[] {
  const entries: Entry[] = [];
  try {
    const source = metadata as { getMap?: () => Record<string, unknown>; get?: (name: string) => unknown[] };
    for (const name of Object.keys(source.getMap?.() ?? {})) for (const value of source.get?.(name) ?? []) if (typeof value === "string") entries.push([name, value]);
  } catch { /* an unreadable metadata object adds nothing */ }
  return entries;
}

const decoded = (text: string): string => { try { return decodeURIComponent(text); } catch { return text; } };
/** The Future AGI key values that may not appear under any other name: the environment's, and the ones the exporter's own options carry. */
function keyValues(intended: Intended): string[] {
  const values = new Set<string>(intended.values);
  for (const name of ["FI_API_KEY", "FI_SECRET_KEY"]) { const value = process.env[name]; if (typeof value === "string" && value.trim().length >= 8) { values.add(value); values.add(value.trim()); } }
  return [...values];
}
/** Whether the final entries carry anything the decision did not put there, and whether they carry any key header at all (a lone one counts). Throws FI_AMBIENT_KEY_HEADERS (naming no value). */
function judgeFinalSet(entries: readonly Entry[], intended: Intended): boolean {
  const values = keyValues(intended);
  const present = new Map<string, string>();
  for (const [name, value] of entries) {
    const key = decoded(name).trim().toLowerCase();
    const isKey = KEY_NAME.test(key);
    if (isKey) {
      // A key header is the decision's own, with the decision's value, or it is not allowed.
      if (intended.keys.get(key) !== value) throw new FiKeyRefusal("FI_AMBIENT_KEY_HEADERS");
      present.set(key, value);
      continue;
    }
    for (const secret of values) {
      if (value.includes(secret) || decoded(value).includes(secret) || value.includes(encodeURIComponent(secret))) throw new FiKeyRefusal("FI_AMBIENT_KEY_HEADERS");
    }
  }
  // A key header, even a lone one, makes the send key-carrying; the set must be the decision's own, no half of it.
  if (present.size > 0 && [...intended.keys.keys()].some(name => !present.has(name))) throw new FiKeyRefusal("FI_AMBIENT_KEY_HEADERS");
  return present.size > 0;
}

// --- the transport -----------------------------------------------------------------------------------------------------

/**
 * Node's own environment-proxy switch, read once when this module loads, from what Node saw at start: the value 1 of NODE_USE_ENV_PROXY (Node
 * accepts no other value), the flag --use-env-proxy, or the same flag in NODE_OPTIONS. A value set later in the process does not turn it on.
 * What cannot be read counts as on.
 */
const ENV_PROXY_AT_START: boolean = (() => {
  try {
    if (process.env.NODE_USE_ENV_PROXY === "1") return true;
    return [...process.execArgv, ...(process.env.NODE_OPTIONS ?? "").split(/\s+/)].some(flag => flag === "--use-env-proxy");
  } catch { return true; }
})();
function nodeEnvProxy(): boolean { return ENV_PROXY_AT_START; }
/**
 * The environment proxy Node would use for https, read once when this module loads and only when Node's switch is on: lower-case
 * `https_proxy` before `HTTPS_PROXY`, and `no_proxy` before `NO_PROXY`, as Node reads them. Nothing set later in the process is read.
 */
const ENV_PROXY: { readonly proxy: string | undefined; readonly noProxy: string } | undefined = (() => {
  try {
    if (!ENV_PROXY_AT_START) return undefined;
    const proxy = process.env.https_proxy ?? process.env.HTTPS_PROXY;
    return Object.freeze({ proxy: proxy === "" ? undefined : proxy, noProxy: process.env.no_proxy ?? process.env.NO_PROXY ?? "" });
  } catch { return undefined; }
})();

/** The one place a channel's credentials are constructed: TLS, or plaintext for a channel the decision allowed. */
export function channelCredentials(kind: "tls" | "plaintext"): grpc.ChannelCredentials {
  return kind === "tls" ? grpc.credentials.createSsl() : grpc.credentials.createInsecure();
}

/**
 * The https proxy of the SDK, or undefined: the `httpsProxy` option, else FI_HTTPS_PROXY, else (only when Node's switch was on at start) the
 * environment's https proxy unless NO_PROXY exempts the destination. The option and FI_HTTPS_PROXY ignore NO_PROXY. Anything but an
 * http:// or https:// URL is refused, naming no value.
 */
export function resolveProxy(target: string, option: unknown): string | undefined {
  const chosen = typeof option === "string" && option !== "" ? option : process.env.FI_HTTPS_PROXY !== undefined && process.env.FI_HTTPS_PROXY !== "" ? process.env.FI_HTTPS_PROXY : undefined;
  let proxy = chosen;
  if (proxy === undefined && ENV_PROXY?.proxy) {
    let exempt = false;
    try { const url = new URL(target); exempt = undiciNoProxyExempts(url.hostname, url.port || (url.protocol === "https:" ? "443" : "80"), ENV_PROXY.noProxy); } catch { exempt = false; }
    if (!exempt) proxy = ENV_PROXY.proxy;
  }
  if (proxy === undefined) return undefined;
  let scheme: string;
  try { scheme = new URL(proxy).protocol; } catch { throw new FiKeyRefusal("FI_PROXY_UNSUPPORTED"); }
  if (scheme !== "http:" && scheme !== "https:") throw new FiKeyRefusal("FI_PROXY_UNSUPPORTED");
  return proxy;
}
/** The one direct transport for key-carrying sends: its own agent, TLS verification always on whatever NODE_TLS_REJECT_UNAUTHORIZED says. */
let directAgent: Dispatcher | undefined;
function direct(): Dispatcher { return directAgent ??= newDirectAgent(); }
/** One CONNECT-only proxy agent per proxy URL: the destination's certificate and the proxy's own (for an https proxy) are always verified. */
const proxyAgents = new Map<string, Dispatcher>();
function through(proxy: string): Dispatcher {
  let agent = proxyAgents.get(proxy);
  if (agent === undefined) { agent = newProxyAgent(proxy); proxyAgents.set(proxy, agent); }
  return agent;
}
/**
 * The dispatcher for a key-carrying request: fi-core's own, never the global slot. A plaintext request goes direct (never through a proxy,
 * loopback included): it is refused when an SDK proxy is configured or Node's environment-proxy switch is on. An https request goes
 * through the SDK's https proxy when one applies, else direct.
 */
function dispatcherFor(target: string, tls: boolean, option: unknown, keys: boolean): Dispatcher {
  // A send that carries no key is not subject to the keyed-send proxy rules: it goes direct, with fi-core's own agent.
  if (!keys) return direct();
  if (!tls) {
    if (nodeEnvProxy() || (typeof option === "string" && option !== "") || (process.env.FI_HTTPS_PROXY ?? "") !== "") throw new FiKeyRefusal("FI_PLAINTEXT_PROXIED");
    return direct();
  }
  const proxy = resolveProxy(target, option);
  return proxy === undefined ? direct() : through(proxy);
}
/** A caller's `dispatcher` option is refused on a key-carrying send: the SDK's own transport is the only one used. */
function assertNoCallerDispatcher(callerDispatcher: unknown): void {
  if (callerDispatcher !== undefined) throw new FiKeyRefusal("FI_DISPATCHER_UNKNOWN");
}

/** The proxy gRPC reads for `host` from `env`, as grpcio reads it: the first SET of grpc_proxy, https_proxy, http_proxy (lower case only; an empty value counts as set and means none), unless no_grpc_proxy, else no_proxy, names the host. */
export function grpcProxyFor(host: string, env: Env = process.env): string | undefined {
  const chosen = ["grpc_proxy", "https_proxy", "http_proxy"].find(name => env[name] !== undefined);
  const proxy = chosen === undefined ? undefined : env[chosen];
  if (!proxy) return undefined;
  const exempt = env.no_grpc_proxy !== undefined ? env.no_grpc_proxy : env.no_proxy;
  if (exempt && matchesNoProxy(host, "", exempt)) return undefined;
  return proxy;
}

// --- the one judging function ---------------------------------------------------------------------------------------------

export interface SendFacts {
  transport: EndpointTransport;
  /** The final url (HTTP) or target (gRPC) of this send. */
  target: string;
  /** The final headers or metadata of this send. */
  entries: readonly Entry[];
  /** What the decision allowed to be on the wire. */
  intended: Intended;
  /** gRPC: the channel is encrypted. */
  secureChannel?: boolean;
  /** A dispatcher the caller supplied (fetch). */
  dispatcher?: unknown;
  allowInsecureHttp?: unknown;
}
export interface SendOutcome { keys: boolean; tls: boolean; plaintextDirect: boolean }

/**
 * Judge one send on its final url and final headers or metadata. Throws FiKeyRefusal, naming no value, when the send must not happen.
 * This is the one function every key-carrying send goes through.
 */
export function guardSend(facts: SendFacts): SendOutcome {
  const keys = judgeFinalSet(facts.entries, facts.intended);
  if (!keys) return { keys: false, tls: facts.transport === "http" ? /^https:/i.test(facts.target.trim()) : facts.secureChannel === true, plaintextDirect: false };
  // The destination rules run again on the final target and the key headers actually present.
  const present: Record<string, string> = {};
  for (const [name, value] of facts.entries) if (KEY_NAME.test(decoded(name).trim())) present[decoded(name).trim().toLowerCase()] = value;
  const request = { target: facts.target, transport: facts.transport, headers: present, allowInsecureHttp: facts.allowInsecureHttp,
    credentials: facts.transport === "grpc" ? channelCredentials(facts.secureChannel ? "tls" : "plaintext") : undefined };
  const decision = decideFiRequest(request);
  // A dispatcher option from the caller is refused for every key-carrying request: the SDK's own transport is the only one used.
  if (facts.transport === "http") assertNoCallerDispatcher(facts.dispatcher);
  return { keys: true, tls: decision.tls, plaintextDirect: decision.plaintextDirect };
}

// --- guarded entry points -------------------------------------------------------------------------------------------------

export interface GuardedFetchRequest {
  url: string;
  init?: RequestInit & { dispatcher?: unknown };
  /** The decision's key headers and values for this request. */
  intended: Intended;
  allowInsecureHttp?: unknown;
  /** The https proxy of the SDK (see `resolveProxy`); wins over FI_HTTPS_PROXY. */
  httpsProxy?: unknown;
}
/**
 * The one fetch for every request that may carry Future AGI keys. The final headers and the proxy exposure are judged right before the
 * send; the send uses fi-core's own pinned undici transport (never the global dispatcher slot or globalThis.fetch), redirects are not
 * followed, and an answer that is a redirect to a request that carried keys is a refusal (its body is dropped).
 * Throws FiKeyRefusal, naming no value, and sends nothing, for a refused send.
 */
export async function guardedFetch(request: GuardedFetchRequest): Promise<Response> {
  const init = { ...(request.init ?? {}) };
  const outcome = guardSend({ transport: "http", target: request.url, entries: headerEntries(init.headers), intended: request.intended,
    dispatcher: init.dispatcher, allowInsecureHttp: request.allowInsecureHttp });
  const dispatcher = dispatcherFor(request.url, outcome.tls, request.httpsProxy, outcome.keys);
  delete init.dispatcher;
  const response = await ownFetch(request.url, { ...(init as object), dispatcher, redirect: "manual" } as Parameters<typeof ownFetch>[1]);
  if (outcome.keys && isRedirect(response)) {
    try { await response.body?.cancel(); } catch { /* the body is not read */ }
    throw new FiKeyRefusal("FI_REDIRECT");
  }
  return response;
}

function unsupported(): never { throw new Error(UNSUPPORTED_EXPORTER_LAYOUT); }
/**
 * An exporter whose send path cannot be wrapped is refused when it carries Future AGI keys, or when the environment holds any: nothing
 * could judge what it sends. An exporter for another collector with no key anywhere in play is left as it is (ambient key headers were
 * already refused at construction), so an unusual exporter version does not stop an application's own exporter.
 */
function unguardable(intended: Intended): void {
  if (intended.keys.size > 0 || keyValues(intended).length > 0) unsupported();
}

/** HTTP statuses the OTLP specification treats as retryable, and the Retry-After header in milliseconds (a count of seconds or an HTTP date). */
const RETRYABLE = new Set([429, 502, 503, 504]);
function retryAfterMillis(value: string | null): number | undefined {
  if (!value) return undefined;
  if (/^\d+$/.test(value.trim())) return Number(value.trim()) * 1000;
  const date = Date.parse(value);
  return Number.isNaN(date) ? undefined : Math.max(0, date - Date.now());
}

/**
 * Guard an OTLP HTTP exporter's send, and make the send fi-core's own: the final url and headers of every request (after the SDK's own,
 * the caller's and the ambient merge) are judged, then the request goes out through fi-core's pinned undici transport (its own agent, or
 * the SDK's https proxy), so no global agent, proxy or dispatcher of the application is used. The exporter's retry layer is kept. A refused
 * send reports a failed export and logs the value-free refusal. When the installed exporter does not have the layout this relies on,
 * construction is refused.
 */
export function guardOtlpHttpExporter(exporter: unknown, intended: Intended, allowInsecureHttp?: unknown, httpsProxy?: unknown): void {
  const transport = (exporter as any)?._delegate?._transport?._transport;
  const parameters = transport?._parameters;
  if (!transport || typeof transport.send !== "function" || !parameters || typeof parameters.headers !== "function" || typeof parameters.url !== "string") return unguardable(intended);
  transport.send = async (data: Uint8Array, timeoutMillis: number) => {
    try {
      const outcome = guardSend({ transport: "http", target: parameters.url, entries: headerEntries(parameters.headers()), intended, allowInsecureHttp });
      const dispatcher = dispatcherFor(parameters.url, outcome.tls, httpsProxy, outcome.keys);
      const url = new URL(parameters.url); url.search = ""; url.hash = ""; url.username = ""; url.password = ""; // the exporter never sent userinfo, and fetch refuses a URL that has it
      const headers: Record<string, string> = { ...parameters.headers() };
      const body = parameters.compression === "gzip" ? gzipSync(data) : data;
      if (parameters.compression === "gzip") headers["Content-Encoding"] = "gzip";
      const response = await ownFetch(url.href, { method: "POST", headers, body, dispatcher, redirect: "manual", signal: AbortSignal.timeout(timeoutMillis) });
      const text = Buffer.from(await response.arrayBuffer());
      if (outcome.keys && isRedirect(response)) throw new FiKeyRefusal("FI_REDIRECT");
      if (response.status < 299) return { status: "success", data: text };
      if (RETRYABLE.has(response.status)) return { status: "retryable", retryInMillis: retryAfterMillis(response.headers.get("retry-after")) };
      return { status: "failure", error: new OTLPExporterError(response.statusText, response.status, text.toString()) };
    } catch (error) {
      if (error instanceof FiKeyRefusal) diag.error(`OTLP HTTP export not sent${failureSuffix(error)}`);
      return { status: "failure", error: error instanceof FiKeyRefusal ? reportedRefusal(error) : new Error(error instanceof Error && error.name === "TimeoutError" ? "Request Timeout" : "OTLP HTTP request failed") };
    }
  };
}

/**
 * Guard an OTLP gRPC exporter: its client is created on first send, here, with a client interceptor that judges the final metadata of
 * every call before anything is connected or sent, and, for a plaintext channel that carries keys, with proxies off
 * (grpc.enable_http_proxy: 0), whatever grpc_proxy, https_proxy and http_proxy say. A TLS channel keeps the normal proxy behaviour.
 * When the installed exporter does not have the layout this relies on, construction is refused.
 */
export function guardOtlpGrpcExporter(exporter: unknown, intended: Intended, allowInsecureHttp?: unknown): void {
  const transport = (exporter as any)?._delegate?._transport;
  const parameters = transport?._parameters;
  if (!transport || typeof transport.send !== "function" || !parameters || typeof parameters.credentials !== "function" || typeof parameters.metadata !== "function"
    || typeof parameters.address !== "string" || typeof parameters.grpcPath !== "string" || typeof parameters.grpcName !== "string") return unguardable(intended);
  const identity = (value: Buffer): Buffer => value;
  const Client = grpc.makeGenericClientConstructor({ export: { path: parameters.grpcPath, requestStream: false, responseStream: false,
    requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity } } as grpc.ServiceDefinition, parameters.grpcName);
  const target = parameters.address as string;
  const secure = (): boolean => { try { return Boolean(parameters.credentials()?._isSecure?.()); } catch { return false; } };
  const interceptor: grpc.Interceptor = (options, nextCall) => {
    let refused = false;
    return new grpc.InterceptingCall(nextCall(options), {
      start(metadata, listener, next) {
        try { guardSend({ transport: "grpc", target, entries: metadataEntries(metadata), intended, secureChannel: secure(), allowInsecureHttp }); }
        catch (error) {
          refused = true;
          diag.error(`OTLP gRPC export not sent${failureSuffix(error)}`);
          listener.onReceiveStatus({ code: grpc.status.PERMISSION_DENIED, details: error instanceof FiKeyRefusal ? refusalSummary(error) : describeFailure(error), metadata: new grpc.Metadata() });
          return;
        }
        next(metadata, listener);
      },
      sendMessage(message, next) { if (!refused) next(message); },
      halfClose(next) { if (!refused) next(); },
    });
  };
  const original = transport.send.bind(transport);
  transport.send = (data: Uint8Array, timeoutMillis: number) => {
    if (transport._client == null) {
      try {
        const metadata = metadataEntries(parameters.metadata());
        // A plaintext channel that carries keys is built with proxies off; the decision is made once here and each call is judged again.
        const carriesKeys = judgeFinalSetQuiet(metadata, intended);
        const plaintext = !secure();
        transport._metadata = parameters.metadata();
        transport._client = new Client(parameters.address, parameters.credentials(), {
          "grpc.default_compression_algorithm": parameters.compression === "gzip" ? 2 : 0,
          ...(plaintext && (carriesKeys || intended.keys.size > 0) ? { "grpc.enable_http_proxy": 0 } : {}),
          interceptors: [interceptor],
        });
      } catch (error) { return Promise.resolve({ status: "failure", error }); }
    }
    return original(data, timeoutMillis);
  };
}
/** Whether the entries carry any key header, a lone one included, without judging them. */
function judgeFinalSetQuiet(entries: readonly Entry[], intended: Intended): boolean {
  const present: Record<string, string> = {};
  for (const [name, value] of entries) if (KEY_NAME.test(decoded(name).trim())) present[decoded(name).trim().toLowerCase()] = value;
  void intended;
  return Object.keys(present).length > 0;
}
