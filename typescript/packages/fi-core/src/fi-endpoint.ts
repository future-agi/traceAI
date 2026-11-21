/**
 * Whether an exporter endpoint names a Future AGI host. The host is the one the exporter will dial, read
 * the way the exporter itself reads its target, so a spelling the exporter would interpret differently
 * (a custom scheme, a userinfo part, an unusually long scheme) cannot pass for a Future AGI host.
 * One trailing dot is part of the same host in every decision. Only the host decides; no environment
 * is consulted here.
 */
const FI_HOST = /(?:^|\.)futureagi\.com$/i;
/** Whether `hostname` (any case, one trailing dot allowed) is futureagi.com or one of its subdomains. */
export function isFiHostName(hostname: string): boolean { return FI_HOST.test(fiHostname(hostname)); }

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

/** Schemes for which the WHATWG URL parser treats a backslash like a slash. Every other scheme keeps it as an ordinary character. */
const SPECIAL_SCHEMES = /^(?:https?|wss?|ftp|file)$/i;

interface WrittenParts {
  /** The effective scheme the parser applies (a scheme-less gRPC target is read as https). */
  scheme: string | undefined;
  /** The authority as written: from after the scheme up to the first separator for that scheme. */
  authority: string;
  /** The text after the authority. */
  rest: string;
  /** The text between the scheme and the first / ? # of any kind: where a backslash or %5C can hide a second host. */
  rawAuthority: string;
}
function writtenParts(endpoint: string, transport: EndpointTransport): WrittenParts | undefined {
  let text = endpoint.trim();
  // gRPC reads a scheme of at most eight word characters, else the whole text is the target; HTTP needs a scheme.
  const match = (transport === "grpc" ? /^(\w{1,8}):\/\// : /^([A-Za-z][A-Za-z0-9+.-]*):\/\//).exec(text);
  let scheme: string | undefined;
  if (match) { scheme = match[1]; text = text.slice(match[0].length); }
  else if (transport === "http") return undefined;
  else scheme = "https";
  const special = SPECIAL_SCHEMES.test(scheme);
  const end = text.search(special ? /[/?#\\]/ : /[/?#]/);
  const authority = end === -1 ? text : text.slice(0, end);
  const raw = text.search(/[/?#]/);
  return { scheme, authority, rest: end === -1 ? "" : text.slice(end), rawAuthority: raw === -1 ? text : text.slice(0, raw) };
}

/**
 * The host as written in the URL or target, before any parser rewrites it (userinfo, port, path, query and fragment
 * removed, lower case, IPv6 brackets kept), or undefined when there is none. A WHATWG URL turns 127.1, 0x7f.0.0.1 and
 * 2130706433 into 127.0.0.1 and drops a trailing dot after an IPv4 address; loopback is decided on what was written.
 * The authority ends at a backslash only for the schemes in which a URL parser treats a backslash as a separator
 * (http, https, ws, wss, ftp, file); for any other scheme a backslash is an ordinary character of the userinfo.
 */
export function writtenHost(endpoint: string, transport: EndpointTransport): string | undefined {
  const parts = writtenParts(endpoint, transport);
  if (!parts) return undefined;
  const hostport = parts.authority.slice(parts.authority.lastIndexOf("@") + 1);
  if (hostport.startsWith("[")) { const close = hostport.indexOf("]"); return close === -1 ? undefined : hostport.slice(0, close + 1).toLowerCase(); }
  const colon = hostport.lastIndexOf(":");
  const host = colon === -1 ? hostport : hostport.slice(0, colon);
  return host === "" ? undefined : host.toLowerCase();
}

/** A host in the one form two spellings of the same host are compared in: lower case, no brackets, one trailing dot removed. */
export function comparableHost(host: string): string { return fiHostname(host.replace(/^\[|\]$/g, "")); }

export interface TargetShape {
  /** The scheme is present (or implied for gRPC) but the authority is empty: `https:///host/` and `https://\\host/` are not re-read from the path. */
  emptyAuthority: boolean;
  /**
   * A second host can hide in the text: a backslash or percent-encoded backslash before the end of the authority, or an `@` after it.
   * Parsers disagree about these, so no decision may rest on one parser's reading.
   */
  ambiguous: boolean;
  /** The written host and the host the exporter dials differ in a Future AGI decision (one is a Future AGI host, the other is not). */
  fiDisagreement: boolean;
}
/** How the target's two readings (as written, as parsed) relate. Both are consulted; neither alone classifies a target. */
export function targetShape(endpoint: string, transport: EndpointTransport): TargetShape {
  const parts = writtenParts(endpoint, transport);
  if (!parts) return { emptyAuthority: false, ambiguous: false, fiDisagreement: false };
  const unix = parts.scheme?.toLowerCase() === "unix";
  const emptyAuthority = !unix && /^(?::\d*)?$/.test(parts.authority);
  const ambiguous = /\\|%5c|%40|@/i.test(parts.rawAuthority) || (parts.rest.includes("@"));
  const written = writtenHost(endpoint, transport), parsed = endpointTarget(endpoint, transport)?.hostname;
  const fiDisagreement = written !== undefined && parsed !== undefined && isFiHostName(written) !== isFiHostName(parsed);
  return { emptyAuthority, ambiguous, fiDisagreement };
}

/** True when the written host and the host the exporter dials are the same host (modulo case, brackets and a trailing dot). */
export function hostsAgree(endpoint: string, transport: EndpointTransport): boolean {
  const written = writtenHost(endpoint, transport), parsed = endpointTarget(endpoint, transport)?.hostname;
  return written !== undefined && parsed !== undefined && comparableHost(written) === comparableHost(parsed);
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);
/** True only for the literal loopback spellings localhost, 127.0.0.1 and ::1 / [::1], as written, when the host the exporter dials is the same host. */
export function isLiteralLoopback(endpoint: string, transport: EndpointTransport): boolean {
  const host = writtenHost(endpoint, transport);
  return host !== undefined && LOOPBACK_HOSTS.has(host) && hostsAgree(endpoint, transport);
}
