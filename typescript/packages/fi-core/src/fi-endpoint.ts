/**
 * Whether an exporter endpoint names a Future AGI host. The host is the one the exporter will dial, read
 * the way the exporter itself reads its target, so a spelling the exporter would interpret differently
 * (a custom scheme, a userinfo part, a trailing dot, an unusually long scheme) cannot pass for a
 * Future AGI host. Only the host decides; no environment is consulted here.
 */
const FI_HOST = /(?:^|\.)futureagi\.com$/i;

export type EndpointTransport = "http" | "grpc";

/** The host and port an OTLP/HTTP exporter dials for `endpoint`, or undefined when it is not an http(s) URL. */
function httpTarget(endpoint: string): { hostname: string; port: string; origin: string } | undefined {
  try {
    const url = new URL(endpoint.trim());
    if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) return undefined;
    const port = url.port || (url.protocol === "http:" ? "80" : "443");
    return { hostname: url.hostname, port, origin: `${url.protocol}//${url.hostname}:${port}` };
  } catch { return undefined; }
}

/**
 * The host and port an OTLP gRPC exporter dials for `endpoint`: its own rule (a scheme of at most eight word
 * characters, else https://, then the URL's host; a unix socket is never a Future AGI host), with gRPC's port 443 by default.
 */
function grpcTarget(endpoint: string): { hostname: string; port: string; origin: string } | undefined {
  try {
    let text = endpoint.trim();
    if (!/^([\w]{1,8}):\/\//.test(text)) text = `https://${text}`;
    const url = new URL(text);
    if (url.protocol === "unix:" || !url.hostname) return undefined;
    const port = url.port || "443";
    return { hostname: url.hostname, port, origin: `${url.hostname}:${port}` };
  } catch { return undefined; }
}

export function endpointTarget(endpoint: string, transport: EndpointTransport): { hostname: string; port: string; origin: string } | undefined {
  return transport === "grpc" ? grpcTarget(endpoint) : httpTarget(endpoint);
}

/** True when the endpoint's dialled host is futureagi.com or one of its subdomains. */
export function isFiEndpoint(endpoint: string, transport: EndpointTransport): boolean {
  const target = endpointTarget(endpoint, transport);
  return target !== undefined && FI_HOST.test(target.hostname);
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
