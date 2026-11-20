/**
 * The one place that decides, for a request that may carry Future AGI keys:
 *   (a) whether the destination is Future AGI bound,
 *   (b) whether the request is encrypted (TLS), and
 *   (c) whether the keys may be attached under the plaintext / opt-in / proxy rules,
 * from the target as WRITTEN and the host the transport will actually dial. The two readings must agree; where they
 * do not (a backslash, a percent-encoded backslash or an `@` that one parser reads one way and another reads another)
 * the request is refused and no key goes anywhere.
 *
 * It is also the only code that reads FI_API_KEY / FI_SECRET_KEY from the environment or writes the key-named
 * headers and metadata. It returns the FINAL outgoing set: the HTTP headers, or the gRPC Metadata with the caller's
 * metadata and headers merged, so the decision is made on what is sent and not on one input. Every attach site calls
 * it before anything is attached (see FI_KEY_ATTACH_SITES.md and fiKeyAttachPoints.test.ts).
 *
 * Refusals are FiKeyRefusal errors: a stable `code` and a message that names no value.
 */
import * as grpc from "@grpc/grpc-js";
import { diag } from "@opentelemetry/api";
import { comparableHost, endpointTarget, fiHostname, isFiEndpoint, isFiHostName, isLiteralLoopback, sameEndpointTarget, targetShape, writtenHost, type EndpointTransport } from "./fi-endpoint";
import { parseKeyPairsIntoRecord } from "@opentelemetry/core";
import { credentialSecrets } from "./privacy-runtime";
import { seedSecrets } from "./secrets";

export type FiKeyRefusalCode = "FI_HOST_MISMATCH" | "FI_EMPTY_AUTHORITY" | "FI_PLAINTEXT_FI_HOST" | "FI_PLAINTEXT_NOT_ALLOWED" | "FI_PLAINTEXT_PROXIED" | "FI_AMBIENT_KEY_HEADERS" | "FI_REDIRECT";
const MESSAGES: Record<FiKeyRefusalCode, string> = {
  FI_HOST_MISMATCH: "The endpoint names its host in a way parsers read differently (a backslash, a percent-encoded backslash or an @ outside the host part), so no Future AGI key is sent to it. Write the endpoint as scheme://host[:port]/path.",
  FI_EMPTY_AUTHORITY: "The endpoint has an empty host (scheme followed by no host), so no Future AGI key is sent to it. Write the endpoint as scheme://host[:port]/path.",
  FI_PLAINTEXT_FI_HOST: "Future AGI keys are sent only over TLS: use an https:// endpoint for a Future AGI host.",
  FI_PLAINTEXT_NOT_ALLOWED: "Future AGI keys are sent over a plaintext channel only to loopback. For a self-hosted collector without TLS, set FI_ALLOW_INSECURE_HTTP=1 or pass allowInsecureHttp: true.",
  FI_PLAINTEXT_PROXIED: "FI_ALLOW_INSECURE_HTTP and allowInsecureHttp do not apply when the request would go through an HTTP or HTTPS proxy; use TLS, or send without a proxy dispatcher.",
  FI_AMBIENT_KEY_HEADERS: "OTEL_EXPORTER_OTLP_HEADERS or OTEL_EXPORTER_OTLP_TRACES_HEADERS carries a Future AGI key header or a known credential, so no exporter is created for this collector. Remove it from those variables, or send the traces to Future AGI.",
  FI_REDIRECT: "The collector answered with a redirect, which is not followed when Future AGI keys are sent; nothing further was sent.",
};
export class FiKeyRefusal extends Error {
  readonly code: FiKeyRefusalCode;
  constructor(code: FiKeyRefusalCode) { super(MESSAGES[code]); this.name = "FiKeyRefusal"; this.code = code; }
}

type FIHeaders = Record<string, string>;
const KEY_NAMES = ["x-api-key", "x-secret-key"] as const;
const isKeyName = (name: string): boolean => (KEY_NAMES as readonly string[]).includes(name.toLowerCase());

/** Whether `headers` carries a complete pair: a non-empty string for each key name, any entry in any case counting. */
export function hasCompleteKeyPair(headers: unknown): boolean {
  if (typeof headers !== "object" || headers === null) return false;
  const present = (name: string): boolean => Object.entries(headers).some(([key, value]) => key.toLowerCase() === name && typeof value === "string" && value.trim() !== "");
  return present("x-api-key") && present("x-secret-key");
}

/** The string-valued entries of a gRPC Metadata object (for secret discovery and the exporter's privacy filter). */
export function metadataStrings(metadata: unknown): FIHeaders {
  const strings: FIHeaders = {};
  try {
    const map = (metadata as { getMap?: () => Record<string, unknown> } | undefined)?.getMap?.() ?? {};
    for (const [name, value] of Object.entries(map)) if (typeof value === "string") strings[name] = value;
  } catch { /* An unreadable metadata object adds nothing. */ }
  return strings;
}
/** Whether a gRPC Metadata carries a complete pair, in any of its (repeated) values. */
function metadataHasPair(metadata: grpc.Metadata): boolean {
  return KEY_NAMES.every(name => metadata.get(name).some(value => typeof value === "string" && value.trim() !== ""));
}
/** Every value of the caller's gRPC Metadata, repeated and binary ones included, into `target`. */
function copyMetadata(metadata: unknown, target: grpc.Metadata): void {
  try {
    const source = metadata as { getMap?: () => Record<string, unknown>; get?: (name: string) => unknown[] } | undefined;
    for (const name of Object.keys(source?.getMap?.() ?? {})) for (const value of source?.get?.(name) ?? []) target.add(name, value as string | Buffer);
  } catch { /* An unreadable metadata object adds nothing. */ }
}

/** The environment's keys, or {} for an incomplete pair (warned once per call when `warn`), or undefined when none is set. */
function readEnvKeys(warn: boolean): FIHeaders | undefined {
  const apiKey = process.env.FI_API_KEY, secretKey = process.env.FI_SECRET_KEY;
  if (apiKey?.trim() && secretKey?.trim()) return { "x-api-key": apiKey, "x-secret-key": secretKey }; // lower case for gRPC
  if (apiKey?.trim() || secretKey?.trim()) {
    if (warn) diag.warn(`Ignoring incomplete FI credentials from environment: missing ${apiKey?.trim() ? "FI_SECRET_KEY" : "FI_API_KEY"}. Supply both in the same source.`);
    return {}; // Resolved empty source: nested constructors must not warn again.
  }
  return undefined;
}

/** The headers an FI-bound exporter sends: the caller's explicit ones, with the environment's keys where the caller supplied no complete pair. */
function fiBoundHeaders(headers: FIHeaders | undefined): FIHeaders | undefined {
  if (headers === undefined) return readEnvKeys(true);
  const items = Object.entries(headers), pair = items.filter(([name]) => isKeyName(name));
  if (!pair.length) return headers; // Explicit bearer/custom/empty headers remain host-owned.
  if (hasCompleteKeyPair(headers)) return headers;
  const key = pair.find(([name]) => name.toLowerCase() === "x-api-key")?.[1], secret = pair.find(([name]) => name.toLowerCase() === "x-secret-key")?.[1];
  if (key?.trim() || secret?.trim()) diag.warn(`Ignoring incomplete FI credentials from explicit headers: missing ${key?.trim() ? "FI_SECRET_KEY" : "FI_API_KEY"}. Supply both in the same source.`);
  return { ...Object.fromEntries(items.filter(([name]) => !isKeyName(name))), ...readEnvKeys(true) };
}

// --- proxies ---------------------------------------------------------------------------------------------------

const escapeRegExp = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/**
 * NO_PROXY matching exactly as Python's urllib.request.proxy_bypass_environment: comma-separated entries, whitespace
 * trimmed, case-insensitive, leading dots ignored, an entry matches the host itself or any subdomain of it
 * (".example.com" matches a.example.com and example.com), "*" bypasses all, an entry with a port ("host:port") matches
 * only that host and port; no CIDR. `port` is the explicit port of the request, or "" when none was written.
 */
export function matchesNoProxy(host: string, port: string, noProxy: string): boolean {
  const hostOnly = host.toLowerCase().replace(/^\[|\]$/g, ""), hostPort = port ? `${hostOnly}:${port}` : hostOnly;
  for (const raw of noProxy.split(",")) {
    const entry = raw.trim().toLowerCase();
    if (!entry) continue;
    if (entry === "*") return true;
    const pattern = new RegExp(`^(?:.+\\.)?${escapeRegExp(entry.replace(/^\.+/, ""))}$`);
    if (pattern.test(hostOnly) || pattern.test(hostPort)) return true;
  }
  return false;
}
type Env = Readonly<Record<string, string | undefined>>;
/**
 * The proxy settings the way Python's urllib.request.getproxies_environment reads them: a name ending in _proxy in any
 * case, the lower-case spelling winning, and an empty lower-case value removing the setting.
 */
export function pythonProxies(env: Env): Record<string, string> {
  const proxies: Record<string, string> = {};
  for (const [name, value] of Object.entries(env)) if (value && name.toLowerCase().endsWith("_proxy")) proxies[name.toLowerCase().slice(0, -6)] = value;
  for (const [name, value] of Object.entries(env)) if (name.endsWith("_proxy") && name === name.toLowerCase()) { if (value) proxies[name.slice(0, -6)] = value; else delete proxies[name.slice(0, -6)]; }
  return proxies;
}
/** Whether Python would send a request to `host` through a proxy: an http or all proxy is set and proxy_bypass_environment does not exempt the host. */
export function proxiedByPython(host: string, port: string, env: Env = process.env): boolean {
  const proxies = pythonProxies(env);
  if (!(proxies.http || proxies.all)) return false;
  return !matchesNoProxy(host, port, proxies.no ?? "");
}
/** Whether undici's EnvHttpProxyAgent would send a request to `host` through a proxy: its own environment reading and its own NO_PROXY rule. */
export function proxiedByUndiciEnvironment(host: string, port: string, env: Env = process.env): boolean {
  const proxy = env.http_proxy || env.HTTP_PROXY;
  if (!proxy) return false;
  const hostname = host.toLowerCase().replace(/^\[|\]$/g, "");
  for (const raw of (env.no_proxy || env.NO_PROXY || "").split(/[,\s]/)) {
    const entry = raw.trim().toLowerCase();
    if (!entry) continue;
    if (entry === "*") return false;
    const m = /^(.*?)(?::(\d+))?$/.exec(entry);
    const name = m?.[1] ?? entry, entryPort = m?.[2];
    if (entryPort && entryPort !== (port || "80")) continue;
    if (!/^[.*]/.test(name)) { if (hostname === name || hostname.endsWith(`.${name}`)) return false; }
    else if (hostname.endsWith(name.replace(/^\*/, ""))) return false;
  }
  return true;
}
const DIRECT_DISPATCHERS = new Set(["Agent", "Pool", "Client", "BalancedPool", "RoundRobinPool", "H2CClient"]);
/** The constructor name of a dispatcher, or undefined when it cannot be read without running its code. */
function dispatcherName(dispatcher: unknown): string | undefined {
  try {
    if (typeof dispatcher !== "object" || dispatcher === null) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(dispatcher, "constructor");
    if (descriptor && !("value" in descriptor)) return undefined; // an accessor: reading it would run the caller's code
    const proto = Object.getPrototypeOf(dispatcher);
    if (proto === null) return undefined;
    const name = (proto as { constructor?: { name?: unknown } }).constructor?.name;
    return typeof name === "string" && name !== "" ? name : undefined;
  } catch { return undefined; }
}
/**
 * Whether a request through `dispatcher` (a caller's, or the global one) counts as proxied: any proxy or environment-reading agent
 * (EnvHttpProxyAgent included) always does, and for a direct dispatcher Python's proxy_bypass_environment decides. A dispatcher that
 * cannot be inspected, or one this code does not know to be direct, counts as proxied.
 */
export function dispatcherProxied(dispatcher: unknown, host: string, port: string, env: Env = process.env): boolean {
  const name = dispatcherName(dispatcher);
  if (name === undefined) return true;
  // A proxy agent, and every agent that may take its proxy from the environment or from an option (EnvHttpProxyAgent), is proxied:
  // what it reads can change between its construction and a send, and an option names a proxy the environment never shows.
  if (/Proxy/.test(name)) return true;
  if (!DIRECT_DISPATCHERS.has(name)) return true;
  return proxiedByPython(host, port, env);
}
/** The dispatcher a global fetch uses right now (undici's global slot), whatever installed it, or undefined when none is set. */
function globalDispatcher(): unknown {
  try { return (globalThis as Record<symbol, unknown>)[Symbol.for("undici.globalDispatcher.1")]; } catch { return undefined; }
}

// --- plaintext rule --------------------------------------------------------------------------------------------

let warnedInsecure = false;
/**
 * The plaintext rule for a request that carries Future AGI keys. Over TLS anything goes (not asked). In plaintext:
 * loopback is allowed; a Future AGI host (one trailing dot stripped) never is; any other host only with
 * FI_ALLOW_INSECURE_HTTP=1 (exactly) or `allowInsecureHttp: true`, with one warning per process. A request the SDK builds
 * itself is then built with proxies off (see the callers); a request through a dispatcher the caller supplies is refused when it
 * would be proxied. Returns whether the opt-in allowed the request.
 */
function plaintextRule(target: string, transport: EndpointTransport, allowInsecureHttp: unknown, dispatcher: unknown): boolean {
  const parsed = endpointTarget(target, transport);
  if (parsed === undefined) return false;
  const host = fiHostname(parsed.hostname), written = writtenHost(target, transport);
  if (isFiHostName(host) || (written !== undefined && isFiHostName(written))) throw new FiKeyRefusal("FI_PLAINTEXT_FI_HOST");
  if (isLiteralLoopback(target, transport)) return false;
  if (allowInsecureHttp !== true && process.env.FI_ALLOW_INSECURE_HTTP !== "1") throw new FiKeyRefusal("FI_PLAINTEXT_NOT_ALLOWED");
  const explicitPort = transport === "http" ? (() => { try { return new URL(target.trim()).port; } catch { return ""; } })() : parsed.port;
  if (dispatcher !== undefined && dispatcherProxied(dispatcher, parsed.hostname, explicitPort)) throw new FiKeyRefusal("FI_PLAINTEXT_PROXIED");
  if (!warnedInsecure) {
    warnedInsecure = true;
    const message = `Sending Future AGI keys in plaintext to ${transport === "http" ? parsed.protocol : "grpc:"}//${parsed.hostname}:${parsed.port} because FI_ALLOW_INSECURE_HTTP or allowInsecureHttp is set.`;
    diag.warn(message);
    try { process.emitWarning(message, { code: "FI_KEYS_PLAINTEXT" }); } catch { /* A host warning handler may throw; the decision stands. */ }
  }
  return true;
}

/**
 * Called by every fetch that carries Future AGI keys, right before it is sent: a plaintext request to a host that is not
 * loopback must not go through a proxy. The dispatcher is the caller's, or else the global one, read now (a global dispatcher
 * can be set at any time), and Node's own environment-proxy switch counts. Throws FiKeyRefusal("FI_PLAINTEXT_PROXIED").
 */
export function assertDirectFetch(url: string, keysAttached: boolean, callerDispatcher?: unknown): void {
  if (!keysAttached) return;
  const parsed = endpointTarget(url, "http");
  if (parsed === undefined || parsed.protocol !== "http:" || isLiteralLoopback(url, "http")) return;
  const port = (() => { try { return new URL(url.trim()).port; } catch { return ""; } })();
  const active = callerDispatcher !== undefined ? callerDispatcher : globalDispatcher();
  // Node's own environment-proxy switch makes the global fetch read the proxy variables itself: such a send is proxied.
  const nodeEnvProxy = callerDispatcher === undefined && ["1", "true"].includes((process.env.NODE_USE_ENV_PROXY ?? "").toLowerCase());
  if (nodeEnvProxy) throw new FiKeyRefusal("FI_PLAINTEXT_PROXIED");
  // An empty global slot is the default Agent: the same rule as a plain Agent installed there (Python's proxy_bypass_environment).
  if (active === undefined ? proxiedByPython(parsed.hostname, port) : dispatcherProxied(active, parsed.hostname, port)) throw new FiKeyRefusal("FI_PLAINTEXT_PROXIED");
}

/** Whether a response is a redirect (3xx): not followed when keys were sent. */
export function isRedirect(response: { status?: number; type?: string } | undefined): boolean {
  return response !== undefined && ((typeof response.status === "number" && response.status >= 300 && response.status < 400) || response.type === "opaqueredirect");
}

// --- ambient OTLP headers --------------------------------------------------------------------------------------

/** The headers an OTLP exporter merges in from OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_TRACES_HEADERS, parsed as the exporter parses them (the traces variable wins). */
function ambientOtlpHeaders(): Record<string, string> {
  return { ...parseKeyPairsIntoRecord(process.env.OTEL_EXPORTER_OTLP_HEADERS?.trim()), ...parseKeyPairsIntoRecord(process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS?.trim()) };
}
/** The values the given headers (an options object, or the strings of gRPC metadata) carry under a Future AGI key header name. */
export function fiKeyHeaderValues(...sources: Array<Record<string, unknown> | undefined>): string[] {
  const found: string[] = [];
  for (const source of sources) for (const [name, value] of Object.entries(source ?? {})) if (isKeyName(name) && typeof value === "string") found.push(value);
  return found;
}
/** The forms a value can take in an ambient header: as written, percent-decoded (the exporter decodes values), and percent-encoded. */
function valueForms(text: string): string[] {
  const forms = [text];
  try { forms.push(decodeURIComponent(text)); } catch { /* not percent-encoded text */ }
  return forms;
}
/**
 * An exporter for a collector that is not Future AGI merges the ambient OTLP headers into what it sends. Refuse (naming no
 * value) when they carry a Future AGI key header, or a Future AGI key value in any header value: `fiKeyValues` (the key values of
 * the exporter's own headers option and metadata) and FI_API_KEY / FI_SECRET_KEY from the environment. Another vendor's values,
 * and secrets that are not Future AGI keys, are that collector's own. Does nothing when no OTEL header variable is set, never reads
 * the secret registry, and skips a value that is not a string. Call it where the exporter reads them, immediately before its constructor.
 */
export function assertNoKeysInAmbientHeaders(fiKeyValues: readonly unknown[] = []): void {
  const raw = [process.env.OTEL_EXPORTER_OTLP_HEADERS, process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS].filter((text): text is string => typeof text === "string" && text.trim() !== "");
  if (raw.length === 0) return;
  const ambient = ambientOtlpHeaders();
  if (Object.keys(ambient).some(isKeyName)) throw new FiKeyRefusal("FI_AMBIENT_KEY_HEADERS");
  const keys = new Set<string>();
  for (const value of [...fiKeyValues, ...["FI_API_KEY", "FI_SECRET_KEY"].map(name => process.env[name])]) if (typeof value === "string" && value.trim().length >= 8) { keys.add(value); keys.add(value.trim()); }
  if (keys.size === 0) return;
  const haystacks = [...raw.flatMap(valueForms), ...Object.values(ambient).filter((value): value is string => typeof value === "string").flatMap(valueForms)];
  for (const key of keys) {
    const needles = [key, encodeURIComponent(key)];
    if (haystacks.some(text => needles.some(needle => text.includes(needle)))) throw new FiKeyRefusal("FI_AMBIENT_KEY_HEADERS");
  }
}

// --- the decider -----------------------------------------------------------------------------------------------

export interface FiRequest {
  /** The target the transport will dial: the exporter's endpoint / url, or the URL of a request. */
  target: string;
  /** The text the target was made from, when it was rewritten on the way (a base URL joined to a path): its shape is checked too. */
  written?: string;
  transport: EndpointTransport;
  /** The caller's explicit headers option. */
  headers?: FIHeaders;
  /** The caller's gRPC Metadata (merged with the headers into the final set). */
  metadata?: unknown;
  /** The caller's gRPC channel credentials. */
  credentials?: unknown;
  /** The `allowInsecureHttp` option; FI_ALLOW_INSECURE_HTTP=1 in the environment does the same. */
  allowInsecureHttp?: unknown;
  /** The SDK's own wiring says the target is the Future AGI route (the auto package resolves its base URL). */
  declaredFi?: boolean;
  /** The endpoint the FI environment (or the default) selects; a target that names the same host and port is Future AGI bound. */
  envRoute?: string;
  /** A caller-supplied undici dispatcher or HTTP agent, when the SDK can see one. */
  dispatcher?: unknown;
}
export interface FiDecision {
  fiBound: boolean;
  /** The final outgoing set carries a complete key pair. */
  keys: boolean;
  /** The request is encrypted: https for HTTP, a secure channel for gRPC. */
  tls: boolean;
  dialedHost: string;
  /** HTTP: the headers to send (FI keys included where they go). gRPC: the headers merged into `metadata`. */
  headers: FIHeaders | undefined;
  /** gRPC only: the final metadata. */
  metadata?: grpc.Metadata;
  /** gRPC only: the channel credentials, or undefined for the exporter's own choice. */
  credentials?: grpc.ChannelCredentials;
  /** A plaintext request that carries keys was allowed by the opt-in: the SDK builds its transport with proxies off. */
  plaintextOptIn: boolean;
}

/**
 * Decide a request and return its final outgoing set. Throws FiKeyRefusal, naming no value, when the keys it would
 * carry may not go to that destination.
 */
export function decideFiRequest(request: FiRequest): FiDecision {
  const { target, transport } = request;
  const shapes = [targetShape(target, transport), ...(request.written !== undefined && request.written !== target ? [targetShape(request.written, transport)] : [])];
  if (shapes.some(shape => shape.emptyAuthority)) throw new FiKeyRefusal("FI_EMPTY_AUTHORITY");
  if (shapes.some(shape => shape.fiDisagreement)) throw new FiKeyRefusal("FI_HOST_MISMATCH");
  const parsed = endpointTarget(target, transport);

  // FI binding: the host the transport dials is a Future AGI host (the written host, checked above, agrees), or the SDK's
  // own route, or the route the FI environment selects.
  const fiBound = request.declaredFi === true || (parsed !== undefined && isFiHostName(parsed.hostname))
    || (request.envRoute !== undefined && sameEndpointTarget(target, request.envRoute, transport));

  // Headers: a complete pair in the caller's metadata is as explicit as one in the headers option.
  const callerStrings = transport === "grpc" ? metadataStrings(request.metadata) : {};
  const requested = transport === "grpc" && request.headers === undefined && hasCompleteKeyPair(callerStrings)
    ? Object.fromEntries(Object.entries(callerStrings).filter(([name]) => isKeyName(name))) : request.headers;
  seedSecrets(process.env, credentialSecrets(requested ?? {})); // ignored halves remain known secrets
  const headers = fiBound ? fiBoundHeaders(requested) : requested;

  // The final outgoing set.
  let metadata: grpc.Metadata | undefined;
  if (transport === "grpc") {
    metadata = new grpc.Metadata();
    copyMetadata(request.metadata, metadata);
    for (const [name, value] of Object.entries(headers ?? {})) metadata.set(name.toLowerCase(), value);
  }
  const keys = metadata ? metadataHasPair(metadata) : hasCompleteKeyPair(headers);

  // A target a second parser reads differently never carries keys.
  if (shapes.some(shape => shape.ambiguous) && (keys || hasCompleteKeyPair(readEnvKeys(false)) || isFiEndpoint(target, transport) || (request.written !== undefined && isFiEndpoint(request.written, transport)))) throw new FiKeyRefusal("FI_HOST_MISMATCH");

  if (transport === "http") {
    const tls = parsed?.protocol === "https:";
    const plaintextOptIn = keys && !tls ? plaintextRule(target, transport, request.allowInsecureHttp, request.dispatcher) : false;
    return { fiBound, keys, tls, dialedHost: parsed ? comparableHost(parsed.hostname) : "", headers, plaintextOptIn };
  }

  // gRPC channel. A Future AGI host always gets TLS, whatever the scheme, the credentials or OTEL_EXPORTER_OTLP_(TRACES_)INSECURE say.
  // Loopback keeps a plaintext channel. Another host gets the channel the options ask for (an http:// scheme, insecure credentials, or
  // the INSECURE environment variable mean plaintext); a plaintext channel that carries keys needs the opt-in.
  const supplied = request.credentials as grpc.ChannelCredentials | undefined;
  const dialedHost = parsed ? comparableHost(parsed.hostname) : "";
  const done = (credentials: grpc.ChannelCredentials | undefined, tls: boolean, plaintextOptIn = false): FiDecision => ({ fiBound, keys, tls, dialedHost, headers, metadata, credentials, plaintextOptIn });
  if (isFiEndpoint(target, transport)) return done(grpc.credentials.createSsl(), true);
  const loopback = isLiteralLoopback(target, transport);
  if (loopback && (fiBound || supplied === undefined)) return done(grpc.credentials.createInsecure(), false);
  const insecureEnv = (process.env.OTEL_EXPORTER_OTLP_TRACES_INSECURE ?? process.env.OTEL_EXPORTER_OTLP_INSECURE)?.toLowerCase() === "true";
  const scheme = /^([\w]{1,8}):\/\//.exec(target.trim())?.[1]?.toLowerCase();
  const plaintext = supplied !== undefined ? !supplied._isSecure() : scheme === "https" ? false : scheme === "http" || insecureEnv;
  if (!keys) return done(supplied ?? undefined, !plaintext);
  if (plaintext) {
    const optIn = !loopback && plaintextRule(target, transport, request.allowInsecureHttp, request.dispatcher);
    return done(supplied ?? grpc.credentials.createInsecure(), false, optIn);
  }
  return done(supplied ?? grpc.credentials.createSsl(), true);
}
