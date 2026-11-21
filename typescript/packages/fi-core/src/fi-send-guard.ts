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
import { type EndpointTransport } from "./fi-endpoint";
import { UNSUPPORTED_EXPORTER_LAYOUT } from "./otlp-header-isolation";
import { decideFiRequest, FiKeyRefusal, hasCompleteKeyPair, isRedirect, matchesNoProxy, type FiDecision } from "./fi-request-policy";

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

// --- the final set -----------------------------------------------------------------------------------------------

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
/** Whether the final entries carry anything the decision did not put there. Throws FI_AMBIENT_KEY_HEADERS (naming no value). */
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

// --- proxies and dispatchers ---------------------------------------------------------------------------------------

/** Node's own environment-proxy switch, by variable or by flag, which makes the global fetch read the proxy variables itself. */
function nodeEnvProxy(): boolean {
  if (["1", "true"].includes((process.env.NODE_USE_ENV_PROXY ?? "").toLowerCase())) return true;
  const flags = [...process.execArgv, ...(process.env.NODE_OPTIONS ?? "").split(/\s+/)];
  return flags.some(flag => flag === "--use-env-proxy");
}
/** An own data property of `object`, by key; an accessor is never run, and what cannot be read as data is undefined. */
function dataProperty(object: unknown, key: PropertyKey): unknown {
  try {
    if (typeof object !== "object" && typeof object !== "function") return undefined;
    if (object === null) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && "value" in descriptor ? descriptor.value : undefined;
  } catch { return undefined; }
}
const GLOBAL_DISPATCHER = Symbol.for("undici.globalDispatcher.1");
type Slot = { kind: "empty" } | { kind: "data"; value: unknown } | { kind: "uninspectable" };
/** The undici global-dispatcher slot, read as an own data property only: an accessor is not run, an inherited value is not read. */
function readSlot(): Slot {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, GLOBAL_DISPATCHER);
    if (descriptor === undefined) return GLOBAL_DISPATCHER in globalThis ? { kind: "uninspectable" } : { kind: "empty" };
    if (!("value" in descriptor)) return { kind: "uninspectable" };
    return descriptor.value === undefined || descriptor.value === null ? { kind: "empty" } : { kind: "data", value: descriptor.value };
  } catch { return { kind: "uninspectable" }; }
}
/**
 * Node's own default dispatcher, by provenance: the object Node itself put in the slot, captured here when this module loads. Node installs
 * it on first use of fetch or Request (an env-proxy agent when NODE_USE_ENV_PROXY or --use-env-proxy is on), so it is made to do so now,
 * before any application code can replace the slot. A slot that already holds something when this module loads, or ever holds anything
 * else, is not Node's own: nothing is accepted on a class name or a shape.
 */
/** The one probe that has Node install its own default dispatcher while the slot is empty: a fetch of a data URL, which opens no connection. */
function primeNodeDispatcher(): void {
  void globalThis.fetch("data:,").catch(() => undefined);
}
/** The one place a channel's credentials are constructed: TLS, or plaintext for a channel the decision allowed. */
export function channelCredentials(kind: "tls" | "plaintext"): grpc.ChannelCredentials {
  return kind === "tls" ? grpc.credentials.createSsl() : grpc.credentials.createInsecure();
}
/** Shared by every copy of this package in the process: the first copy to load captures Node's own default, later copies read it. */
const SHARED_DEFAULT = Symbol.for("traceai.fi-core.node-default-dispatcher");
const nodeDefault: { value: unknown } = (() => {
  try {
    const shared = dataProperty(globalThis, SHARED_DEFAULT) as { value: unknown } | undefined;
    if (shared !== undefined && typeof shared === "object" && "value" in shared) return shared;
    const loaded = readSlot();
    // With Node's environment-proxy switch on, Node installs its env-proxy agent while it starts, so a data slot at this point is that
    // agent (nothing can show who wrote it, and the switch is the case this transport exists for). Without the switch, a slot that is
    // already set belongs to the application.
    let captured: { value: unknown };
    if (loaded.kind !== "empty") captured = { value: loaded.kind === "data" && nodeEnvProxy() ? loaded.value : undefined };
    else {
      primeNodeDispatcher();
      const slot = readSlot();
      captured = { value: slot.kind === "data" ? slot.value : undefined };
    }
    try { Object.defineProperty(globalThis, SHARED_DEFAULT, { value: Object.freeze(captured), enumerable: false, writable: false, configurable: false }); } catch { /* another copy defined it */ }
    return captured;
  } catch { return { value: undefined }; }
})();
/**
 * The dispatcher check for a key-carrying request, run for plaintext and https alike. A caller's `dispatcher` option is refused. For a fetch
 * the SDK sends itself, the global slot must be empty or hold the very object Node installed (read as an own data property); anything else
 * (a proxy agent, an Agent, a pool, a Proxy object, a subclass, an accessor, an inherited value) is refused as FI_DISPATCHER_UNKNOWN. A
 * plaintext request is never proxied, so NODE_USE_ENV_PROXY and --use-env-proxy refuse it.
 */
function assertDispatcher(tls: boolean, callerDispatcher: unknown, ownFetch: boolean): void {
  if (callerDispatcher !== undefined) throw new FiKeyRefusal("FI_DISPATCHER_UNKNOWN");
  if (!ownFetch) return;
  const slot = readSlot();
  if (slot.kind === "uninspectable" || (slot.kind === "data" && (nodeDefault.value === undefined || slot.value !== nodeDefault.value))) throw new FiKeyRefusal("FI_DISPATCHER_UNKNOWN");
  if (!tls && nodeEnvProxy()) throw new FiKeyRefusal("FI_PLAINTEXT_PROXIED");
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
  /** The request is a fetch the SDK builds itself (not through a dispatcher of the caller's): its proxy exposure is the global dispatcher and Node's switches. */
  fetch?: boolean;
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
  // The dispatcher check runs for every key-carrying request, https included: a dispatcher whose dial can be redirected is unknown and refused,
  // and a plaintext request never goes through a proxy, loopback included. The OTLP HTTP exporter sends with an agent of its own (no dispatcher,
  // no proxy environment), so only a fetch or a request through a caller's dispatcher is checked.
  if (facts.transport === "http" && (facts.fetch === true || facts.dispatcher !== undefined)) {
    assertDispatcher(decision.tls, facts.dispatcher, facts.fetch === true);
  }
  return { keys: true, tls: decision.tls, plaintextDirect: decision.plaintextDirect };
}

// --- guarded entry points -------------------------------------------------------------------------------------------------

export interface GuardedFetchRequest {
  url: string;
  init?: RequestInit & { dispatcher?: unknown };
  /** The decision's key headers and values for this request. */
  intended: Intended;
  allowInsecureHttp?: unknown;
}
/**
 * The one fetch for every request that may carry Future AGI keys. The final headers and the proxy exposure are judged right before the
 * send; redirects are not followed, and an answer that is a redirect to a request that carried keys is a refusal (its body is dropped).
 * Throws FiKeyRefusal, naming no value, and sends nothing, for a refused send.
 */
export async function guardedFetch(request: GuardedFetchRequest): Promise<Response> {
  const init = { ...(request.init ?? {}) };
  const outcome = guardSend({ transport: "http", target: request.url, entries: headerEntries(init.headers), intended: request.intended,
    dispatcher: init.dispatcher, allowInsecureHttp: request.allowInsecureHttp, fetch: true });
  const response = await fetch(request.url, { ...init, redirect: "manual" });
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

/**
 * Guard an OTLP HTTP exporter's send: the final url and headers of every request (after the SDK's own, the caller's and the ambient
 * merge) are judged before the exporter's own transport sends them. A refused send reports a failed export and logs the value-free refusal.
 * When the installed exporter does not have the layout this relies on, construction is refused.
 */
export function guardOtlpHttpExporter(exporter: unknown, intended: Intended, allowInsecureHttp?: unknown): void {
  const transport = (exporter as any)?._delegate?._transport?._transport;
  const parameters = transport?._parameters;
  if (!transport || typeof transport.send !== "function" || !parameters || typeof parameters.headers !== "function" || typeof parameters.url !== "string") return unguardable(intended);
  const original = transport.send.bind(transport);
  transport.send = (data: Uint8Array, timeoutMillis: number) => {
    try {
      // The global dispatcher slot is checked for this send too, though the exporter's own agent does not read it.
      guardSend({ transport: "http", target: parameters.url, entries: headerEntries(parameters.headers()), intended, allowInsecureHttp, fetch: true });
    } catch (error) {
      diag.error(`OTLP HTTP export not sent: ${describeFailure(error)}`);
      return Promise.resolve({ status: "failure", error });
    }
    return original(data, timeoutMillis);
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
          diag.error(`OTLP gRPC export not sent: ${describeFailure(error)}`);
          listener.onReceiveStatus({ code: grpc.status.PERMISSION_DENIED, details: describeFailure(error), metadata: new grpc.Metadata() });
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
/** Whether the entries carry a complete key pair, without judging them. */
function judgeFinalSetQuiet(entries: readonly Entry[], intended: Intended): boolean {
  const present: Record<string, string> = {};
  for (const [name, value] of entries) if (KEY_NAME.test(decoded(name).trim())) present[decoded(name).trim().toLowerCase()] = value;
  void intended;
  return hasCompleteKeyPair(present);
}
