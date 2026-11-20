/**
 * Whether an exporter endpoint names a Future AGI host. The host is the one the exporter will dial, read
 * the way the exporter itself reads its target, so a spelling the exporter would interpret differently
 * (a custom scheme, a userinfo part, an unusually long scheme) cannot pass for a Future AGI host.
 * One trailing dot is part of the same host in every decision. Only the host decides; no environment
 * is consulted here.
 */
import { diag } from "@opentelemetry/api";

const FI_HOST = /(?:^|\.)futureagi\.com$/i;

/** The host in the one form every Future AGI host decision compares: lower case, one trailing dot removed. */
export function fiHostname(hostname: string): string {
  const host = hostname.toLowerCase();
  return host.endsWith(".") ? host.slice(0, -1) : host;
}

export type EndpointTransport = "http" | "grpc";

/** The host and port an OTLP/HTTP exporter dials for `endpoint`, or undefined when it is not an http(s) URL. */
function httpTarget(endpoint: string): { hostname: string; port: string; origin: string; protocol: string } | undefined {
  try {
    const url = new URL(endpoint.trim());
    if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) return undefined;
    const port = url.port || (url.protocol === "http:" ? "80" : "443");
    return { hostname: url.hostname, port, origin: `${url.protocol}//${fiHostname(url.hostname)}:${port}`, protocol: url.protocol };
  } catch { return undefined; }
}

/**
 * The host and port an OTLP gRPC exporter dials for `endpoint`: its own rule (a scheme of at most eight word
 * characters, else https://, then the URL's host; a unix socket is never a Future AGI host), with gRPC's port 443 by default.
 */
function grpcTarget(endpoint: string): { hostname: string; port: string; origin: string; protocol?: string } | undefined {
  try {
    let text = endpoint.trim();
    if (!/^([\w]{1,8}):\/\//.test(text)) text = `https://${text}`;
    const url = new URL(text);
    if (url.protocol === "unix:" || !url.hostname) return undefined;
    const port = url.port || "443";
    return { hostname: url.hostname, port, origin: `${fiHostname(url.hostname)}:${port}` };
  } catch { return undefined; }
}

export function endpointTarget(endpoint: string, transport: EndpointTransport): { hostname: string; port: string; origin: string; protocol?: string } | undefined {
  return transport === "grpc" ? grpcTarget(endpoint) : httpTarget(endpoint);
}

/** True when the endpoint's dialled host is futureagi.com or one of its subdomains. */
export function isFiEndpoint(endpoint: string, transport: EndpointTransport): boolean {
  const target = endpointTarget(endpoint, transport);
  if (target === undefined) return false;
  const written = writtenHost(endpoint, transport);
  return FI_HOST.test(fiHostname(target.hostname)) || (written !== undefined && FI_HOST.test(fiHostname(written)));
}

/** True when both spellings dial the same host and port. */
export function sameEndpointTarget(a: string, b: string, transport: EndpointTransport): boolean {
  const left = endpointTarget(a, transport), right = endpointTarget(b, transport);
  return left !== undefined && right !== undefined && left.origin === right.origin;
}

/**
 * The host as written in the URL or target, before any parser rewrites it (userinfo, port, path, query and fragment
 * removed, lower case, IPv6 brackets kept), or undefined when there is none. A WHATWG URL turns 127.1, 0x7f.0.0.1 and
 * 2130706433 into 127.0.0.1 and drops a trailing dot after an IPv4 address; loopback is decided on what was written.
 */
export function writtenHost(endpoint: string, transport: EndpointTransport): string | undefined {
  let text = endpoint.trim();
  // gRPC reads a scheme of at most eight word characters, else the whole text is the target; HTTP needs a scheme.
  const scheme = (transport === "grpc" ? /^\w{1,8}:\/\// : /^[A-Za-z][A-Za-z0-9+.-]*:\/\//).exec(text);
  if (scheme) text = text.slice(scheme[0].length);
  else if (transport === "http") return undefined;
  const end = text.search(/[/?#\\]/);
  const authority = end === -1 ? text : text.slice(0, end);
  const hostport = authority.slice(authority.lastIndexOf("@") + 1);
  if (hostport.startsWith("[")) { const close = hostport.indexOf("]"); return close === -1 ? undefined : hostport.slice(0, close + 1).toLowerCase(); }
  const colon = hostport.lastIndexOf(":");
  const host = colon === -1 ? hostport : hostport.slice(0, colon);
  return host === "" ? undefined : host.toLowerCase();
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);
/** True only for the literal loopback spellings localhost, 127.0.0.1 and ::1 / [::1], as written. */
export function isLiteralLoopback(endpoint: string, transport: EndpointTransport): boolean {
  const host = writtenHost(endpoint, transport);
  return host !== undefined && LOOPBACK_HOSTS.has(host);
}

const PROXY_ENV = ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "GRPC_PROXY", "grpc_proxy"];
/** Whether a request to `hostname:port` would go through a proxy named by the environment, honouring NO_PROXY. */
function viaProxy(hostname: string, port: string): boolean {
  const env = process.env;
  if (!PROXY_ENV.some(name => (env[name] ?? "").trim() !== "")) return false;
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  for (const raw of (env.NO_PROXY ?? env.no_proxy ?? "").split(",")) {
    const entry = raw.trim().toLowerCase();
    if (!entry) continue;
    if (entry === "*") return false;
    const colon = entry.lastIndexOf(":");
    const [name, entryPort] = colon > 0 && !entry.endsWith("]") ? [entry.slice(0, colon), entry.slice(colon + 1)] : [entry, ""];
    if (entryPort && entryPort !== port) continue;
    const domain = name.replace(/^\*?\./, "").replace(/^\[|\]$/g, "");
    if (host === domain || host.endsWith(`.${domain}`)) return false;
  }
  return true;
}

export interface FiKeyGuardOptions {
  /** Set for a gRPC target whose channel is not TLS; an HTTP target is plaintext when its scheme is http. */
  plaintext?: boolean;
  /** The `allowInsecureHttp` option; the environment's FI_ALLOW_INSECURE_HTTP=1 is read here. */
  allowInsecureHttp?: unknown;
}

let warnedInsecure = false;
/**
 * The one rule for a request that carries Future AGI keys. Over TLS anything goes. In plaintext: loopback is
 * allowed; a Future AGI host (one trailing dot stripped) never is; any other host only with
 * FI_ALLOW_INSECURE_HTTP=1 (exactly) or `allowInsecureHttp: true`, never through a proxy, with one warning per
 * process. Throws, naming no value, when the request is refused.
 */
export function guardFiKeys(endpoint: string, transport: EndpointTransport, options: FiKeyGuardOptions = {}): void {
  const target = endpointTarget(endpoint, transport);
  if (target === undefined) return;
  const plaintext = transport === "http" ? target.protocol === "http:" : options.plaintext === true;
  if (!plaintext) return;
  const host = fiHostname(target.hostname);
  const written = writtenHost(endpoint, transport);
  if (FI_HOST.test(host) || (written !== undefined && FI_HOST.test(fiHostname(written)))) throw new Error("Future AGI keys are sent only over TLS: use an https:// endpoint for a Future AGI host.");
  if (isLiteralLoopback(endpoint, transport)) return;
  if (options.allowInsecureHttp !== true && process.env.FI_ALLOW_INSECURE_HTTP !== "1") {
    throw new Error("Future AGI keys are sent over a plaintext channel only to loopback. For a self-hosted collector without TLS, set FI_ALLOW_INSECURE_HTTP=1 or pass allowInsecureHttp: true.");
  }
  if (viaProxy(target.hostname, target.port)) {
    throw new Error("FI_ALLOW_INSECURE_HTTP and allowInsecureHttp do not apply when the request would go through an HTTP or HTTPS proxy; list the collector in NO_PROXY or use TLS.");
  }
  if (!warnedInsecure) {
    warnedInsecure = true;
    diag.warn(`Sending Future AGI keys in plaintext to ${transport === "http" ? target.protocol : "grpc:"}//${target.hostname}:${target.port} because FI_ALLOW_INSECURE_HTTP or allowInsecureHttp is set.`);
  }
}
