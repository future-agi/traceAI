/**
 * Whether an exporter endpoint names a Future AGI host. The host is the one the exporter will dial, read
 * the way the exporter itself reads its target, so a spelling the exporter would interpret differently
 * (a custom scheme, a userinfo part, an unusually long scheme) cannot pass for a Future AGI host.
 * One trailing dot is part of the same host in every decision. Only the host decides; no environment
 * is consulted here.
 */
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
  return target !== undefined && FI_HOST.test(fiHostname(target.hostname));
}

/**
 * The one rule for sending Future AGI keys: an http:// endpoint on a Future AGI host is not sent them. A gRPC
 * Future AGI target is always connected with TLS (see otel.ts), so only the HTTP transport can break the rule.
 */
export function fiKeysRefused(endpoint: string, transport: EndpointTransport): boolean {
  if (transport !== "http") return false;
  const target = httpTarget(endpoint);
  return target !== undefined && target.protocol === "http:" && FI_HOST.test(fiHostname(target.hostname));
}

/** Throws, naming no value, when `endpoint` is one the keys are not sent to (see fiKeysRefused). */
export function guardFiKeys(endpoint: string, transport: EndpointTransport): void {
  if (fiKeysRefused(endpoint, transport)) throw new Error("Future AGI keys are sent only over TLS: use an https:// endpoint for a Future AGI host.");
}

/** True when both spellings dial the same host and port. */
export function sameEndpointTarget(a: string, b: string, transport: EndpointTransport): boolean {
  const left = endpointTarget(a, transport), right = endpointTarget(b, transport);
  return left !== undefined && right !== undefined && left.origin === right.origin;
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
/** True when the gRPC target dials exactly localhost, 127.0.0.1 or ::1 (decided on the parsed host, not on the text). */
export function isLoopbackTarget(endpoint: string): boolean {
  const target = grpcTarget(endpoint);
  return target !== undefined && LOOPBACK_HOSTS.has(target.hostname.toLowerCase());
}
