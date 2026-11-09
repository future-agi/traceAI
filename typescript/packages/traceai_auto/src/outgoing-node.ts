import { Agent as HttpAgent, ClientRequest, OutgoingMessage } from "node:http";
import { Agent as HttpsAgent } from "node:https";
import { isProxy } from "node:util/types";
import { context, type Context } from "@opentelemetry/api";
import type { IntegrationStatus } from "./libraries";
import { allowsOutgoingDestination, normalizePropagationHost, type OutgoingPolicy } from "./outgoing-policy";
import type { HeaderPairs, OutgoingHeaderName, OutgoingPrivacy } from "./outgoing-privacy";
import { outgoingRedirectClosed, closeOutgoingRedirect, outgoingRedirectContext } from "./outgoing-redirect";
import { validOutgoingField, validOutgoingSuffix } from "./outgoing-w3c";

const MAX_FIELDS = 1024, MAX_BYTES = 65536;
const token = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const absent = Symbol("absent");
function own(object: unknown, key: PropertyKey): unknown {
  if (!object || typeof object !== "object" || isProxy(object)) throw new Error("Unsupported outgoing metadata");
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  if (!descriptor) return absent;
  if (!("value" in descriptor)) throw new Error("Unsupported outgoing metadata");
  return descriptor.value;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value) || isProxy(value) || Object.isFrozen(value) || value.length > MAX_FIELDS * 2) throw new Error("Unsupported outgoing carrier");
  const result: unknown[] = [];
  for (let i = 0; i < value.length; i++) { const entry = own(value, String(i)); if (entry === absent) throw new Error("Sparse outgoing carrier"); result.push(entry); }
  return result;
}
interface Carrier { pairs: HeaderPairs; append(values: ReadonlyMap<OutgoingHeaderName, string>): unknown }
function fields() {
  const pairs: [string, string][] = [];
  let bytes = 0;
  return { pairs, add(name: unknown, raw: unknown): string | number | (string | number)[] {
    if (typeof name !== "string" || !token.test(name)) throw new Error("Unsupported header name");
    const values = Array.isArray(raw) ? array(raw) : [raw];
    // Presence, not truthiness: even an empty application value array owns a field.
    if (!values.length) {
      if ((bytes += Buffer.byteLength(name) + 4) > MAX_BYTES || pairs.length >= MAX_FIELDS) throw new Error("Unsupported header budget");
      pairs.push([name, ""]);
    }
    for (const value of values) {
      if (!(typeof value === "string" || typeof value === "number" && Number.isFinite(value))) throw new Error("Unsupported header value");
      const text = String(value);
      if (/[\x00-\x08\x0a-\x1f\x7f]/.test(text) || (bytes += Buffer.byteLength(name) + Buffer.byteLength(text) + 4) > MAX_BYTES || pairs.length >= MAX_FIELDS) throw new Error("Unsupported header budget");
      pairs.push([name, text]);
    }
    return Array.isArray(raw) ? values as (string | number)[] : raw as string | number;
  } };
}
/** Copies only bounded, data-only native carriers. Never mutates caller arrays. */
function nativeCarrier(headers: unknown): Carrier {
  const scan = fields();
  if (headers === undefined || headers === null) return { pairs: scan.pairs, append(values) { return [...values]; } };
  if (Array.isArray(headers)) {
    const entries = array(headers);
    if (entries.length && Array.isArray(entries[0])) {
      const copied = entries.map(entry => {
        const pair = array(entry); if (pair.length !== 2) throw new Error("Unsupported header pair");
        return [pair[0], scan.add(pair[0], pair[1])];
      });
      return { pairs: scan.pairs, append(values) { return [...copied, ...values]; } };
    }
    if (entries.length % 2) throw new Error("Unpaired headers");
    const copied: unknown[] = [];
    for (let i = 0; i < entries.length; i += 2) copied.push(entries[i], scan.add(entries[i], entries[i + 1]));
    return { pairs: scan.pairs, append(values) { return [...copied, ...[...values].flat()]; } };
  }
  if (typeof headers !== "object" || isProxy(headers) || Object.isFrozen(headers)) throw new Error("Unsupported native headers");
  const proto = Object.getPrototypeOf(headers);
  if (proto !== null && proto !== Object.prototype) throw new Error("Unsupported native headers prototype");
  const names = Reflect.ownKeys(headers);
  if (names.length > MAX_FIELDS) throw new Error("Unsupported header budget");
  // Node recognizes its private map by identity with request[kOutHeaders]. A
  // cloned map takes _storeHeader's PUBLIC object branch, serializing [name,value]
  // as the value. Use its supported tuple branch instead, without swapping the
  // request's private map or changing getHeader()/caller-owned data.
  const copied: unknown[][] = [];
  for (const name of names) {
    if (typeof name !== "string") throw new Error("Unsupported native header key");
    const pair = array(own(headers, name));
    if (pair.length !== 2 || typeof pair[0] !== "string" || pair[0].toLowerCase() !== name.toLowerCase()) throw new Error("Unsupported native header pair");
    copied.push([pair[0], scan.add(pair[0], pair[1])]);
  }
  return { pairs: scan.pairs, append(values) { return [...copied, ...values]; } };
}
/** Read only the already-serialized native early-header block, without replaying
 * Node's serializer or treating a logical request origin as transport authority.
 */
function serializedNativeHeaders(headers: string): HeaderPairs {
  const scan = fields();
  if (Buffer.byteLength(headers) > MAX_BYTES || headers && !headers.endsWith("\r\n")) throw new Error("Unsupported native header block");
  for (const line of headers ? headers.slice(0, -2).split("\r\n") : []) {
    const at = line.indexOf(":"); if (at < 1) throw new Error("Unsupported native header line");
    scan.add(line.slice(0, at), line.slice(at + 1).trim());
  }
  return scan.pairs;
}
function authority(value: string, scheme: string): URL | undefined {
  if (!value || value.length > 2048 || /[\s/@\\?#]/.test(value)) return;
  const u = new URL(`${scheme}//${value}/`);
  if (!normalizePropagationHost(u.hostname) || u.username || u.password) return;
  return u;
}
function sameAuthority(a: URL, b: URL): boolean {
  const host = normalizePropagationHost(a.hostname);
  return !!host && a.protocol === b.protocol && host === normalizePropagationHost(b.hostname) && a.port === b.port;
}
function sameOrigin(a: URL, b: URL): boolean {
  // Normalize for every authority/policy check, but DNS equivalence is not proof
  // of HTTP origin identity. WHATWG retains a terminal DNS dot in URL.origin;
  // removing it here would reopen attributable cross-origin redirect chains.
  return sameAuthority(a, b) && a.origin === b.origin;
}
/** A literal transport path is NOT permission to replace its transport origin.
 * WHATWG treats slash-backslash as a network-path reference. Reject that and
 * other parser ambiguities before resolving, then verify authority afterwards.
 */
function resolvedTarget(path: string, base: URL, absolute = false): URL | undefined {
  if (!path || path.length > MAX_BYTES || /[\\\s#]/.test(path)) return;
  const isAbsolute = /^https?:\/\//i.test(path);
  if (isAbsolute ? !absolute : !path.startsWith("/") || path.startsWith("//")) return;
  const resolved = new URL(path, base);
  if (!sameOrigin(resolved, base) || resolved.username || resolved.password) return;
  // Keep the verified authority (including its effective port) authoritative.
  const destination = new URL(base.href);
  destination.pathname = resolved.pathname; destination.search = resolved.search;
  return destination;
}
function nativeDestination(request: unknown, firstLine: unknown, pairs: HeaderPairs, transport: URL | undefined): URL | undefined {
  if (!transport || typeof firstLine !== "string" || firstLine.length > MAX_BYTES) return;
  const line = /^([A-Z!#$%&'*+.^_`|~-]+) ([^\r\n ]+) HTTP\/1\.[01]\r\n$/.exec(firstLine);
  if (!line || line[1] === "CONNECT" || own(request, "socketPath") !== absent && own(request, "socketPath") !== undefined) return;
  const protocol = own(request, "protocol"), rawHost = own(request, "host"), method = own(request, "method");
  if ((protocol !== "http:" && protocol !== "https:") || protocol !== transport.protocol || typeof rawHost !== "string" || method !== line[1]) return;
  if (transport.username || transport.password || transport.pathname !== "/" || transport.search || transport.hash) return;
  const nativeHost = normalizePropagationHost(rawHost.includes(":") && !rawHost.startsWith("[") ? `[${rawHost}]` : rawHost);
  if (!nativeHost || nativeHost !== normalizePropagationHost(transport.hostname)) return;
  const hosts = pairs.filter(([key]) => key.toLowerCase() === "host");
  if (hosts.length !== 1) return;
  const explicit = authority(hosts[0][1], protocol);
  // Host may omit or override a port. Equality of hostnames alone proves nothing
  // about the native connection's port (ClientRequest has no own port slot).
  if (!explicit || !sameOrigin(explicit, transport)) return;
  return resolvedTarget(line[2], transport, true);
}
export interface NativeStage { readonly headers: unknown; readonly additions: ReadonlyMap<OutgoingHeaderName, string> }
/** Internal carrier-only seam. A native adapter MUST supply a transport authority
 * captured from normalized connection options, never reconstructed from Host.
 * No authority means no additions; synthetic carrier tests supply it explicitly.
 */
export function stageNativeOutgoingHeaders(request: unknown, firstLine: unknown, headers: unknown, ctx: Context, policy: OutgoingPolicy, privacy: OutgoingPrivacy, transport?: URL): NativeStage | undefined {
  try {
    const carrier = nativeCarrier(headers), destination = nativeDestination(request, firstLine, carrier.pairs, transport);
    if (!destination || !allowsOutgoingDestination(policy, destination) || outgoingRedirectClosed(ctx)) return;
    const prepared = privacy.prepareOutgoingHeaders(ctx, destination, carrier.pairs, policy);
    if (!prepared?.size || prepared.size > 3) return;
    // Snapshot once and validate before EITHER native serialization path. No
    // non-ASCII byte or field outside W3C can reach the latin1 early-header path.
    const additions = new Map<OutgoingHeaderName, string>();
    for (const [name, value] of prepared) {
      if (additions.size >= 3 || additions.has(name) || !validOutgoingField(name, value) || carrier.pairs.some(([key]) => key.toLowerCase() === name)) return;
      additions.set(name, value);
    }
    if (!additions.size) return;
    return { headers: carrier.append(additions), additions };
  } catch { return; }
}
const nativePendingNoop = own(new OutgoingMessage(), "_onPendingData");
const nativeSocketGetter = Object.getOwnPropertyDescriptor(OutgoingMessage.prototype, "socket")?.get;
/** Constructor-time Expect queues one unsent header before addRequest. Updating
 * _header alone cannot repair that queue. Admit ONLY that exact pristine queue,
 * still without a socket/body, and update its byte count as well as both copies.
 * No serializer replay, callback replacement, queue reordering or host-hook call.
 */
export function appendUnsentNativeHeader(request: object, header: string, suffix: string): boolean {
  if (!validOutgoingSuffix(suffix)) return false;
  const descriptor = Object.getOwnPropertyDescriptor(request, "_header"), output = own(request, "outputData");
  if (!descriptor || !("value" in descriptor) || !descriptor.writable || descriptor.value !== header ||
      !Array.isArray(output) || isProxy(output)) return false;
  const sent = own(request, "_headerSent"), next = header.slice(0, -2) + suffix + "\r\n";
  if (sent === false && output.length === 0) {
    Object.defineProperty(request, "_header", { ...descriptor, value: next }); return true;
  }
  if (sent !== true || output.length !== 1 || isProxy(request)) return false;
  const slot = own(output, "0");
  if (!slot || typeof slot !== "object" || isProxy(slot)) return false;
  const chunk = Object.getOwnPropertyDescriptor(slot, "data"), size = Object.getOwnPropertyDescriptor(request, "outputSize");
  const socket = own(request, "socket"), actualSocket = socket === absent && nativeSocketGetter ? Reflect.apply(nativeSocketGetter, request, []) : socket;
  const encoding = own(slot, "encoding");
  if (actualSocket !== null && actualSocket !== undefined ||
      !chunk || !("value" in chunk) || !chunk.writable || chunk.value !== header ||
      !size || !("value" in size) || !size.writable || size.value !== header.length ||
      encoding !== undefined && encoding !== null && encoding !== "latin1" ||
      own(request, "_onPendingData") !== nativePendingNoop) return false;
  // All targets are non-proxy objects with prevalidated writable data properties.
  // ClientRequest's stock pending-data hook is a no-op; do not invoke app code in
  // this transaction. Unknown accounting hooks fail closed above.
  Object.defineProperty(slot, "data", { ...chunk, value: next });
  Object.defineProperty(request, "outputSize", { ...size, value: size.value + suffix.length });
  Object.defineProperty(request, "_header", { ...descriptor, value: next });
  return true;
}

// Only stock HTTP/HTTPS agents have a known option-resolution contract here.
// No request constructor/request()/get() replacement: cached entry points and
// ESM bindings still reach these prototype seams.
const httpAgentProto = HttpAgent.prototype as any, httpsAgentProto = HttpsAgent.prototype as any;
const stockMethods = {
  addRequest: httpAgentProto.addRequest,
  createSocket: httpAgentProto.createSocket, reuseSocket: httpAgentProto.reuseSocket,
  httpConnect: httpAgentProto.createConnection, httpsConnect: httpsAgentProto.createConnection,
  httpName: httpAgentProto.getName, httpsName: httpsAgentProto.getName,
};
function dataMethod(object: object, key: string): unknown {
  let cursor: object | null = object;
  for (let i = 0; cursor && i < 4; i++, cursor = Object.getPrototypeOf(cursor)) {
    const value = own(cursor, key); if (value !== absent) return value;
  }
  return absent;
}
function optionData(object: unknown): Record<string, unknown> {
  if (!object || typeof object !== "object" || isProxy(object)) throw new Error("Unsupported connection options");
  const proto = Object.getPrototypeOf(object);
  if (proto !== null && proto !== Object.prototype) throw new Error("Unsupported connection options");
  const keys = Reflect.ownKeys(object);
  if (keys.length > MAX_FIELDS) throw new Error("Unsupported connection options");
  const copy = Object.create(null);
  for (const key of keys) {
    const descriptor = Object.getOwnPropertyDescriptor(object, key)!;
    if (!("value" in descriptor)) throw new Error("Unsupported connection options");
    if (typeof key === "string" && descriptor.enumerable) copy[key] = descriptor.value;
  }
  return copy;
}
function nativeTransportAuthority(agent: unknown, request: unknown, options: unknown, addRequest: unknown): URL | undefined {
  if (!agent || typeof agent !== "object" || isProxy(agent)) return;
  const proto = Object.getPrototypeOf(agent), secure = proto === httpsAgentProto;
  if (!secure && proto !== httpAgentProto || own(request, "agent") !== agent) return;
  if (dataMethod(agent, "addRequest") !== addRequest || dataMethod(agent, "createSocket") !== stockMethods.createSocket ||
      dataMethod(agent, "reuseSocket") !== stockMethods.reuseSocket ||
      dataMethod(agent, "createConnection") !== (secure ? stockMethods.httpsConnect : stockMethods.httpConnect) ||
      dataMethod(agent, "getName") !== (secure ? stockMethods.httpsName : stockMethods.httpName)) return;
  // Built-in proxy agents and queued requests can resolve again AFTER this seam.
  // Their final transport is not established here, so never add to those requests.
  if (Reflect.ownKeys(agent).some(key => typeof key === "symbol" && key.description === "kProxyConfig")) return;
  if (Reflect.ownKeys(request as object).some(key => typeof key === "symbol" && key.description === "kRequestOptions" && own(request, key) != null)) return;
  const merged = { ...optionData(options), ...optionData(own(agent, "options")) };
  if (merged.socketPath != null || merged.path != null || merged.socket != null || merged.createConnection != null || merged.proxyEnv != null) return;
  const protocol = own(request, "protocol"), scheme = secure ? "https:" : "http:";
  if (protocol !== scheme || merged.protocol != null && merged.protocol !== scheme) return;
  const host = merged.host, port = merged.port;
  if (typeof host !== "string" || !(typeof port === "number" || typeof port === "string" && /^[0-9]{1,5}$/.test(port))) return;
  const number = Number(port);
  if (!Number.isInteger(number) || number < 1 || number > 65535) return;
  const spelling = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  const canonical = normalizePropagationHost(spelling);
  if (!canonical) return;
  // Keep the platform's hostname (including a terminal DNS dot) for origin
  // identity. The dot-stripped policy key must never replace the captured URL.
  const captured = authority(`${spelling}:${number}`, scheme);
  if (!captured) return;
  // hostname is normally retained in normalized ClientRequest options. Any
  // disagreement introduced by an agent is ambiguous across net/TLS consumers.
  if (merged.hostname != null) {
    if (typeof merged.hostname !== "string") return;
    const hostname = merged.hostname.includes(":") && !merged.hostname.startsWith("[") ? `[${merged.hostname}]` : merged.hostname;
    if (normalizePropagationHost(hostname) !== canonical) return;
    const alias = authority(`${hostname}:${number}`, scheme);
    if (!alias || !sameOrigin(alias, captured)) return;
  }
  return captured;
}

/** Inspect raw data before application response listeners can follow Location.
 * Unknown/ambiguous redirect origins fail closed. Never follow a URL here.
 */
function crossesRedirectOrigin(response: unknown, requestUrl?: URL): boolean {
  try {
    const status = own(response, "statusCode");
    if (typeof status !== "number" || status < 300 || status > 399) return false;
    const raw = array(own(response, "rawHeaders")), locations: string[] = [];
    if (raw.length % 2) return true;
    let bytes = 0;
    for (let i = 0; i < raw.length; i += 2) {
      const name = raw[i], value = raw[i + 1];
      if (typeof name !== "string" || typeof value !== "string" || (bytes += name.length + value.length) > MAX_BYTES) return true;
      if (name.toLowerCase() === "location") locations.push(value);
    }
    // A 304 without Location is not a redirect. A redirect with missing or
    // duplicate Location cannot establish a safe same-origin follow-up.
    if (!locations.length) return [301, 302, 303, 307, 308].includes(status);
    if (locations.length !== 1 || !requestUrl) return true;
    const location = locations[0];
    if (!location || location.length > 8192 || /[^\x21-\x7e]|\\/.test(location)) return true;
    // http:host/path and http:/host/path are relative with a same-scheme base,
    // but native request(string) treats them as absolute. Such references (and
    // extra authority slashes) cannot establish a safe same-origin follow-up.
    if (/^[a-z][a-z0-9+.-]*:/i.test(location) && !/^https?:\/\/[^/]/i.test(location) || location.startsWith("///")) return true;
    const target = new URL(location, requestUrl);
    return !!target.username || !!target.password || !sameOrigin(target, requestUrl);
  } catch { return true; }
}

/** Experimental opt-in, not a claim of runtime qualification. The default
 * remains closed (including direct internal callers); the public configuration
 * must explicitly select adapters independently of destination policy. Carrier
 * guards, per-request authority checks and FI admission still apply to every request.
 * Only stock node:http/node:https routes are supported.
 */
export function installOutgoingPropagation(policy: OutgoingPolicy, privacy: OutgoingPrivacy, statuses: IntegrationStatus[], warn: (key: string, message: string) => void, adaptersEnabled = false): () => void {
  const optedIn = adaptersEnabled === true;
  let active = true;
  const removers: (() => void)[] = [];
  const status = (name: string, supported: boolean): IntegrationStatus => {
    const row: IntegrationStatus = { name, status: policy.mode === "none" || !policy.valid ? "disabled" : supported ? "pending" : "disabled", reason: policy.mode === "none" || !policy.valid ? "Outgoing propagation policy disabled" : supported ? "Awaiting a successful header commit" : "Outgoing runtime qualification has not been completed" };
    statuses.push(row); return row;
  };
  const nativeStatus = status("outgoing-http", optedIn);
  if (!optedIn || policy.mode === "none" || !policy.valid) return () => { active = false; };
  {
    const nativeRemovers: (() => void)[] = [];
    let nativeReady = false;
    const authorities = new WeakMap<object, URL>();
    // Proven serialized targets, not mutable request options or Host-derived
    // guesses. Kept only by weak request identity and never logged/exported.
    const requestUrls = new WeakMap<object, URL>();
    const early = new WeakMap<object, { line: string; header: string; ctx: Context }>();
    const chains = new WeakMap<object, { contexts: Context[]; closed: boolean }>();
    const remember = (request: object, ctx: Context): boolean => {
      let chain = chains.get(request);
      if (!chain) { chain = { contexts: [], closed: false }; chains.set(request, chain); }
      if (!chain.contexts.includes(ctx)) {
        if (chain.contexts.length < 3) chain.contexts.push(ctx);
        else chain.closed = true; // Unbounded reserialization is not a known route.
      }
      chain.closed = chain.closed || outgoingRedirectClosed(ctx) || chain.contexts.some(outgoingRedirectClosed);
      if (chain.closed) { chain.contexts.forEach(closeOutgoingRedirect); closeOutgoingRedirect(ctx); }
      return chain.closed;
    };
    const report = (request: object, staged: NativeStage) => {
      const committed = own(request, "_header");
      if (typeof committed === "string" && committed.length <= MAX_BYTES + 8192 && [...staged.additions].every(([key, value]) => committed.split("\r\n").some(line => line.slice(0, key.length + 2).toLowerCase() === `${key}: ` && line.slice(key.length + 2) === value))) {
        nativeStatus.status = "instrumented"; nativeStatus.reason = "Outgoing headers committed without client spans";
      }
    };
    try {
      const proto = ClientRequest.prototype as any, previous = Object.getOwnPropertyDescriptor(proto, "_storeHeader");
      const original = proto._storeHeader;
      const previousEmit = Object.getOwnPropertyDescriptor(proto, "emit"), originalEmit = proto.emit;
      if (typeof originalEmit !== "function" || previousEmit && (!("value" in previousEmit) || !previousEmit.configurable)) throw new Error("Unsupported native response method");
      const previousAdd = Object.getOwnPropertyDescriptor(httpAgentProto, "addRequest");
      if (typeof original !== "function" || previous && (!("value" in previous) || !previous.configurable) ||
          !previousAdd || !("value" in previousAdd) || !previousAdd.configurable || typeof previousAdd.value !== "function" || previousAdd.value !== stockMethods.addRequest) throw new Error("Unsupported native commit method");
      const wrapper = function(this: object, ...args: unknown[]) {
        let adjusted = args, staged: NativeStage | undefined, pendingContext: Context | undefined, requestUrl: URL | undefined;
        if (active && nativeReady) {
          try {
            const transport = authorities.get(this), ctx = context.active();
            const closed = remember(this, ctx);
            if (transport) {
              requestUrl = nativeDestination(this, args[0], nativeCarrier(args[1]).pairs, transport);
              if (!closed) staged = stageNativeOutgoingHeaders(this, args[0], args[1], ctx, policy, privacy, transport);
            } else if (!closed) pendingContext = ctx;
            if (staged) { adjusted = args.slice(); adjusted[1] = staged.headers; }
          } catch { /* Delegate exactly once, unchanged. */ }
        }
        // Never catch a host failure and retry it or replace its error identity.
        const result = Reflect.apply(original, this, adjusted);
        if (active && nativeReady) {
          try {
            if (requestUrl) requestUrls.set(this, requestUrl); else requestUrls.delete(this);
            if (staged) report(this, staged);
            else if (pendingContext && typeof args[0] === "string") {
              const header = own(this, "_header");
              if (typeof header === "string" && header.length <= MAX_BYTES && header.startsWith(args[0]) && header.endsWith("\r\n\r\n")) early.set(this, { line: args[0], header, ctx: pendingContext });
            }
          } catch { /* Reporting never changes transport behavior. */ }
        }
        return result;
      };
      const addWrapper = function(this: object, request: object, ...args: unknown[]) {
        let before: URL | undefined;
        if (active && nativeReady) {
          try {
            remember(request, context.active());
            before = nativeTransportAuthority(this, request, args[0], addWrapper);
          } catch { /* Unknown routes stay unchanged. */ }
        }
        // Stock addRequest resolves options and schedules onSocket. It can throw;
        // never swallow/retry a host error or claim authority for a failed call.
        const result = Reflect.apply(previousAdd.value, this, [request, ...args]);
        if (active && nativeReady) {
          try {
            const pending = early.get(request); early.delete(request);
            const after = before && nativeTransportAuthority(this, request, args[0], addWrapper);
            if (!before || !after || !sameOrigin(before, after)) return result;
            authorities.set(request, after);
            if (pending && !remember(request, pending.ctx)) {
              // Raw/Expect headers serialize DURING construction, before any port
              // is observable. Append only after transport proof, before sending,
              // without invoking the serializer twice or rewriting existing bytes.
              const headers = serializedNativeHeaders(pending.header.slice(pending.line.length, -2));
              const requestUrl = nativeDestination(request, pending.line, headers, after);
              if (requestUrl) requestUrls.set(request, requestUrl);
              const staged = stageNativeOutgoingHeaders(request, pending.line, headers, pending.ctx, policy, privacy, after);
              if (staged) {
                const suffix = [...staged.additions].map(([key, value]) => `${key}: ${value}\r\n`).join("");
                if (appendUnsentNativeHeader(request, pending.header, suffix)) report(request, staged);
              }
            }
          } catch { /* No changes when the transport authority cannot be proven. */ }
        }
        return result;
      };
      const emitWrapper = function(this: object, ...args: unknown[]) {
        let dispatchContext: Context | undefined;
        if (active && nativeReady && args[0] === "response") {
          try {
            const ctx = context.active(), chain = chains.get(this);
            if (chain?.closed || chain?.contexts.some(outgoingRedirectClosed) || outgoingRedirectClosed(ctx) || crossesRedirectOrigin(args[1], requestUrls.get(this))) {
              if (chain) { chain.closed = true; chain.contexts.forEach(closeOutgoingRedirect); }
              dispatchContext = outgoingRedirectContext(ctx);
            }
          } catch { /* Never expose response metadata or change application errors. */ }
        }
        // Execute exactly once OUTSIDE the inspection catch. Listener exceptions
        // and EventEmitter's return value/order belong to the application.
        const dispatch = () => Reflect.apply(originalEmit, this, args);
        return dispatchContext ? context.with(dispatchContext, dispatch) : dispatch();
      };
      const replace = (target: object, key: string, value: unknown, prior: PropertyDescriptor | undefined) => {
        Object.defineProperty(target, key, prior ? { ...prior, value } : { configurable: true, writable: true, enumerable: false, value });
        nativeRemovers.push(() => {
          if (Object.getOwnPropertyDescriptor(target, key)?.value !== value) return;
          if (prior) Object.defineProperty(target, key, prior); else Reflect.deleteProperty(target, key);
        });
      };
      replace(proto, "emit", emitWrapper, previousEmit);
      replace(proto, "_storeHeader", wrapper, previous);
      replace(httpAgentProto, "addRequest", addWrapper, previousAdd);
      nativeReady = true;
      removers.push(() => { nativeReady = false; for (const remove of nativeRemovers.slice().reverse()) { try { remove(); } catch { /* Retained wrappers are inert. */ } } });
    } catch {
      nativeReady = false;
      for (const remove of nativeRemovers.reverse()) { try { remove(); } catch { /* Roll back only our hooks. */ } }
      nativeStatus.status = "failed"; nativeStatus.reason = "Outgoing native adapter installation failed";
      try { warn("outgoing-http", "Outgoing native propagation unavailable; requests remain unchanged."); } catch { /* Constant diagnostics only. */ }
    }
  }
  return () => {
    if (!active) return; active = false;
    for (const remove of removers.reverse()) { try { remove(); } catch { /* Retained foreign wrappers remain inert. */ } }
  };
}
