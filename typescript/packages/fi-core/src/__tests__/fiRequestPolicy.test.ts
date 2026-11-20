import * as grpc from "@grpc/grpc-js";
import "../fi-route";
import { checkCustomEvalConfigExists, createSpanExporter, FITracerProvider, register, Transport } from "../otel";
import { decideFiRequest, matchesNoProxy } from "../fi-request-policy";
import { EvalName, EvalSpanKind, EvalTagType, ModelChoices } from "../fi_types";

/**
 * One table drives the one decider (decideFiRequest) and, end to end, each transport (createSpanExporter over HTTP and
 * gRPC). A row has the shape of the cross-language fixtures: input target, transport, key source, opt-in, proxy
 * environment -> decision, error code, dialled host, TLS.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const ENV_KEYS = { FI_API_KEY: "env-key-synthetic-0002", FI_SECRET_KEY: "env-secret-synthetic-0002" };
const NAMES = ["OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "FI_API_KEY", "FI_SECRET_KEY",
  "FI_GRPC_COLLECTOR_ENDPOINT", "FI_GRPC_URL", "FI_BASE_URL", "FI_TRACES_URL", "FI_COLLECTOR_ENDPOINT", "FI_ALLOW_INSECURE_HTTP",
  "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "GRPC_PROXY", "grpc_proxy", "no_grpc_proxy"];
let saved: Record<string, string | undefined> = {};
beforeEach(() => { saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; } });
afterEach(() => { for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } });

type Source = "explicit" | "env" | "metadata" | "metadata+empty-headers" | "split" | "none";
interface Row {
  name: string; target: string; transport: "http" | "grpc"; keys: Source; optIn?: "env" | "option"; proxy?: Record<string, string>;
  /** the target is the one the FI environment selects (FI_BASE_URL / FI_GRPC_COLLECTOR_ENDPOINT) */
  fiEnv?: boolean;
  /** a dispatcher the caller supplies, by its undici class name (the decider reads the name): the proxy rules then apply */
  dispatcher?: string;
  expect: { decision: "allow" | "refuse"; code?: string; host?: string; tls?: boolean; keysSent?: boolean };
}
const A = (name: string, target: string, keys: Source, expectation: Row["expect"], more: Partial<Row> = {}): Row => ({ name, target, transport: "http", keys, expect: expectation, ...more });
const G = (name: string, target: string, keys: Source, expectation: Row["expect"], more: Partial<Row> = {}): Row => ({ name, target, transport: "grpc", keys, expect: expectation, ...more });
const PROXY = "http://proxy.internal.example:3128";
const ok = (host: string, tls: boolean, keysSent = true) => ({ decision: "allow" as const, host, tls, keysSent });
const no = (code: string) => ({ decision: "refuse" as const, code });

// A request the SDK builds itself is built with proxies off, so no proxy environment refuses it; the rows below this table
// apply the proxy rules to a request that goes through a dispatcher the caller supplies.
const ROWS: Row[] = [
  // TLS, loopback, Future AGI host
  A("https FI host, env keys", "https://api.futureagi.com", "env", ok("api.futureagi.com", true)),
  A("https FI host, explicit keys", "https://api.futureagi.com/tracer/v1/traces", "explicit", ok("api.futureagi.com", true)),
  A("http FI host", "http://api.futureagi.com", "explicit", no("FI_PLAINTEXT_FI_HOST")),
  A("http FI host with the opt-in", "http://api.futureagi.com", "explicit", no("FI_PLAINTEXT_FI_HOST"), { optIn: "option" }),
  A("http FI host with env keys", "http://api.futureagi.com", "env", no("FI_PLAINTEXT_FI_HOST"), { optIn: "env" }),
  A("http loopback 127.0.0.1", "http://127.0.0.1:4318", "explicit", ok("127.0.0.1", false)),
  A("http loopback localhost", "http://localhost:4318", "explicit", ok("localhost", false)),
  A("https self-hosted", "https://collector.example:4318", "explicit", ok("collector.example", true)),
  A("http self-hosted, no keys", "http://collector.example:4318", "none", ok("collector.example", false, false)),
  // loopback is decided on the host as written: these spellings are other hosts, whatever the URL parser normalises them to
  A("127.1 is not loopback", "http://127.1:4318", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  A("0x7f.0.0.1 is not loopback", "http://0x7f.0.0.1:4318", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  A("2130706433 is not loopback", "http://2130706433:4318", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  A("0177.0.0.1 is not loopback", "http://0177.0.0.1:4318", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  A("127.0.0.1. (trailing dot) is not loopback", "http://127.0.0.1.:4318", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  A("a Future AGI host with a trailing dot over plaintext fails closed", "http://api.futureagi.com.", "explicit", no("FI_PLAINTEXT_FI_HOST"), { optIn: "env" }),
  // plaintext opt-in
  A("http self-hosted, no opt-in", "http://collector.example:4318", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  A("http self-hosted, env opt-in", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env" }),
  A("http self-hosted, option opt-in", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "option" }),
  A("http self-hosted env route, env keys, no opt-in", "http://collector.example:4318", "env", no("FI_PLAINTEXT_NOT_ALLOWED"), { fiEnv: true }),
  A("http self-hosted env route, env keys, opt-in", "http://collector.example:4318", "env", ok("collector.example", false), { fiEnv: true, optIn: "env" }),
  // the one proxy rule
  A("HTTP_PROXY", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY } }),
  A("http_proxy", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { http_proxy: PROXY } }),
  A("ALL_PROXY alone", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { ALL_PROXY: PROXY } }),
  A("all_proxy alone", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { all_proxy: PROXY } }),
  A("HTTPS_PROXY alone does not route http", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTPS_PROXY: PROXY } }),
  A("https_proxy alone does not route http", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { https_proxy: PROXY } }),
  A("empty HTTP_PROXY is not a proxy", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: "  " } }),
  A("NO_PROXY exact host", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: "collector.example" } }),
  A("NO_PROXY leading dot", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: ".example" } }),
  A("NO_PROXY leading dot matches the bare domain", "http://example.com:4318", "explicit", ok("example.com", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: ".example.com" } }),
  A("NO_PROXY *", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: "*" } }),
  A("NO_PROXY host:port", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: "other.test, Collector.Example:4318 " } }),
  A("NO_PROXY non-matching port", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: "collector.example:9999" } }),
  A("NO_PROXY other host", "http://collector.example:4318", "explicit", ok("collector.example", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: "other.example" } }),
  A("NO_PROXY is not CIDR", "http://203.0.113.7:4318", "explicit", ok("203.0.113.7", false), { optIn: "env", proxy: { HTTP_PROXY: PROXY, NO_PROXY: "203.0.113.0/24" } }),
  A("loopback built by the SDK goes direct with HTTP_PROXY set", "http://127.0.0.1:4318", "explicit", ok("127.0.0.1", false), { proxy: { HTTP_PROXY: PROXY } }),
  A("[::1] built by the SDK goes direct with HTTP_PROXY set", "http://[::1]:4318", "explicit", ok("::1", false), { proxy: { HTTP_PROXY: PROXY } }),
  // written vs parsed host: finding 1/2 shapes and the cross-language shapes, on HTTP
  A("backslash before @ (special scheme)", "http://collector.example\\@api.futureagi.com", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
  A("percent-encoded backslash before @", "http://collector.example%5C@api.futureagi.com", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
  A("percent-encoded backslash, lower case", "https://api.futureagi.com%5c@collector.example/", "env", no("FI_HOST_MISMATCH")),
  A("@ inside the path", "http://collector.example/x@api.futureagi.com", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
  A("@ inside the path, https, env keys", "https://api.futureagi.com/x@collector.example", "env", no("FI_HOST_MISMATCH")),
  A("empty authority (three slashes)", "https:///api.futureagi.com/", "env", no("FI_EMPTY_AUTHORITY")),
  A("empty authority (backslash)", "https://\\api.futureagi.com/", "env", no("FI_EMPTY_AUTHORITY")),
  // gRPC
  G("grpc FI host is TLS", "grpc.futureagi.com:443", "env", ok("grpc.futureagi.com", true)),
  G("grpc http:// FI host is still TLS", "http://grpc.futureagi.com:4317", "explicit", ok("grpc.futureagi.com", true), { optIn: "option" }),
  G("grpc loopback", "localhost:4317", "explicit", ok("localhost", false)),
  G("grpc self-hosted TLS", "collector.example:4317", "explicit", ok("collector.example", true)),
  G("grpc self-hosted plaintext, no opt-in", "http://collector.example:4317", "explicit", no("FI_PLAINTEXT_NOT_ALLOWED")),
  G("grpc self-hosted plaintext, opt-in", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env" }),
  G("grpc env-named target keeps its http:// prefix", "http://collector.example:4317", "env", no("FI_PLAINTEXT_NOT_ALLOWED"), { fiEnv: true }),
  G("grpc env-named target with the opt-in", "http://collector.example:4317", "env", ok("collector.example", false), { fiEnv: true, optIn: "env" }),
  // gRPC: finding 3, the final set is merged metadata + headers
  G("metadata pair only", "http://collector.example:4317", "metadata", no("FI_PLAINTEXT_NOT_ALLOWED")),
  G("metadata pair with headers: {}", "http://collector.example:4317", "metadata+empty-headers", no("FI_PLAINTEXT_NOT_ALLOWED")),
  G("pair split across metadata and headers", "http://collector.example:4317", "split", no("FI_PLAINTEXT_NOT_ALLOWED")),
  G("metadata pair with headers: {} and the opt-in", "http://collector.example:4317", "metadata+empty-headers", ok("collector.example", false), { optIn: "env" }),
  G("split pair through a gRPC proxy", "http://collector.example:4317", "split", ok("collector.example", false), { optIn: "env", proxy: { http_proxy: PROXY } }),
  G("metadata pair with headers: {} to a FI host is TLS", "http://grpc.futureagi.com:4317", "metadata+empty-headers", ok("grpc.futureagi.com", true)),
  // gRPC proxy: the channel's own environment (grpc_proxy, https_proxy, http_proxy, lower case) decides
  G("grpc_proxy", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env", proxy: { grpc_proxy: PROXY } }),
  G("https_proxy routes a gRPC channel", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env", proxy: { https_proxy: PROXY } }),
  G("http_proxy routes a gRPC channel", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env", proxy: { http_proxy: PROXY } }),
  G("GRPC_PROXY upper case is not read by gRPC", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env", proxy: { GRPC_PROXY: PROXY } }),
  G("ALL_PROXY is not read by gRPC", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env", proxy: { ALL_PROXY: PROXY } }),
  G("no_proxy bypasses the gRPC proxy", "http://collector.example:4317", "explicit", ok("collector.example", false), { optIn: "env", proxy: { grpc_proxy: PROXY, no_proxy: "collector.example" } }),
  // gRPC: findings 1/2 and the cross-language shapes
  G("FI host with backslash then @ evil, env keys", "grpc://grpc.futureagi.com\\@evil.example:4317", "env", no("FI_HOST_MISMATCH")),
  G("FI host with backslash then @ evil, explicit keys", "grpc://grpc.futureagi.com\\@evil.example:4317", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
  G("localhost with backslash then @ evil", "grpc://localhost\\@evil.example:4317", "explicit", no("FI_HOST_MISMATCH")),
  G("localhost with backslash then @ evil, opt-in", "grpc://localhost\\@evil.example:4317", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
  G("percent-encoded backslash before @", "grpc://collector.example%5C@api.futureagi.com:4317", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
  G("@ inside the path", "http://collector.example:4317/x@grpc.futureagi.com", "explicit", no("FI_HOST_MISMATCH"), { optIn: "env" }),
];

class Agent {}
class EnvHttpProxyAgent {}
class ProxyAgent {}
const DISPATCHERS: Record<string, new () => object> = { Agent, EnvHttpProxyAgent, ProxyAgent };
// An environment-reading dispatcher (EnvHttpProxyAgent) or a proxy agent is always proxied, loopback included: what it reads can change,
// and an option can name a proxy. A plain Agent never proxies on its own, so the NO_PROXY rows below use one and Python's
// proxy_bypass_environment decides them; a row's outcome is the one its name states.
const D = (name: string, target: string, expectation: Row["expect"], proxy: Record<string, string>, dispatcher = "Agent"): Row =>
  ({ name: `${name}, through a ${dispatcher}`, target, transport: "http", keys: "explicit", optIn: "env", proxy, dispatcher, expect: dispatcher === "EnvHttpProxyAgent" || dispatcher === "ProxyAgent" ? no("FI_PLAINTEXT_PROXIED") : expectation });
const COLLECTOR = "http://collector.example:4318";
/** Through a dispatcher the caller supplies: any proxy or environment-reading agent is proxied; for a direct one Python's proxy_bypass_environment decides; unknown counts as proxied. */
const DISPATCHER_ROWS: Row[] = [
  D("HTTP_PROXY", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY }),
  D("HTTP_PROXY", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY }, "EnvHttpProxyAgent"),
  D("http_proxy", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { http_proxy: PROXY }),
  D("ALL_PROXY alone", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { ALL_PROXY: PROXY }),
  D("all_proxy alone", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { all_proxy: PROXY }),
  D("HTTPS_PROXY alone does not route http", COLLECTOR, ok("collector.example", false), { HTTPS_PROXY: PROXY }),
  D("no proxy at all", COLLECTOR, ok("collector.example", false), {}),
  D("no proxy at all", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), {}, "EnvHttpProxyAgent"),
  D("no proxy at all", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), {}, "ProxyAgent"),
  D("NO_PROXY exact host", COLLECTOR, ok("collector.example", false), { HTTP_PROXY: PROXY, NO_PROXY: "collector.example" }),
  D("NO_PROXY leading dot", COLLECTOR, ok("collector.example", false), { HTTP_PROXY: PROXY, NO_PROXY: ".example" }),
  D("NO_PROXY leading dot matches the bare domain", "http://example.com:4318", ok("example.com", false), { HTTP_PROXY: PROXY, NO_PROXY: ".example.com" }),
  D("NO_PROXY *", COLLECTOR, ok("collector.example", false), { HTTP_PROXY: PROXY, NO_PROXY: "*" }),
  D("NO_PROXY host:port", COLLECTOR, ok("collector.example", false), { HTTP_PROXY: PROXY, NO_PROXY: "other.test, Collector.Example:4318 " }),
  D("NO_PROXY non-matching port is refused", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY, NO_PROXY: "collector.example:9999" }),
  D("NO_PROXY other host is refused", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY, NO_PROXY: "other.example" }),
  D("NO_PROXY is not CIDR, so the host is refused", "http://203.0.113.7:4318", no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY, NO_PROXY: "203.0.113.0/24" }),
  D("an empty lower-case no_proxy removes the upper-case one, so the host is refused", COLLECTOR, no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY, NO_PROXY: "collector.example", no_proxy: "" }),
  // Loopback keeps only the opt-in exemption: every proxy rule still runs.
  D("loopback 127.0.0.1", "http://127.0.0.1:4318", no("FI_PLAINTEXT_PROXIED"), {}, "ProxyAgent"),
  D("loopback 127.0.0.1", "http://127.0.0.1:4318", no("FI_PLAINTEXT_PROXIED"), { HTTP_PROXY: PROXY }, "EnvHttpProxyAgent"),
  D("loopback localhost", "http://localhost:4318", no("FI_PLAINTEXT_PROXIED"), {}, "EnvHttpProxyAgent"),
  D("loopback [::1]", "http://[::1]:4318", no("FI_PLAINTEXT_PROXIED"), {}, "ProxyAgent"),
  D("loopback 127.0.0.1 with HTTP_PROXY set is direct", "http://127.0.0.1:4318", ok("127.0.0.1", false), { HTTP_PROXY: PROXY }),
  D("loopback [::1] with HTTP_PROXY set is direct", "http://[::1]:4318", ok("::1", false), { HTTP_PROXY: PROXY }),
];

/** What the transport the exporter built will actually send and dial. */
function outgoing(exporter: unknown, transport: "http" | "grpc"): { headers: Record<string, string>; tls: boolean; host: string } {
  const parameters = (exporter as any)._delegate._transport._parameters;
  if (transport === "grpc") {
    const metadata = parameters.metadata() as grpc.Metadata;
    const headers: Record<string, string> = {};
    for (const [name, values] of Object.entries(metadata.getMap())) headers[name.toLowerCase()] = String(values);
    return { headers, tls: parameters.credentials()._isSecure(), host: String(parameters.address).replace(/:\d+$/, "") };
  }
  const http = (exporter as any)._delegate._transport._transport._parameters;
  const headers = Object.fromEntries(Object.entries(http.headers() as Record<string, string>).map(([name, value]) => [name.toLowerCase(), value]));
  const url = new URL(http.url);
  return { headers, tls: url.protocol === "https:", host: url.hostname.replace(/^\[|\]$/g, "") };
}
const sentKeys = (headers: Record<string, string>): boolean => Boolean(headers["x-api-key"]) && Boolean(headers["x-secret-key"]);
const never = (headers: Record<string, string>) => expect(JSON.stringify(headers)).not.toMatch(/evil\.example/);

function setup(row: Row): { options: Record<string, unknown>; metadata?: grpc.Metadata; headers?: Record<string, string> } {
  Object.assign(process.env, row.proxy ?? {});
  if (row.optIn === "env") process.env.FI_ALLOW_INSECURE_HTTP = "1";
  if (row.keys === "env" || row.fiEnv) Object.assign(process.env, ENV_KEYS);
  let headers: Record<string, string> | undefined;
  let metadata: grpc.Metadata | undefined;
  if (row.keys === "explicit") headers = { ...KEYS };
  if (row.keys === "metadata" || row.keys === "metadata+empty-headers") { metadata = new grpc.Metadata(); for (const [k, v] of Object.entries(KEYS)) metadata.set(k, v); }
  if (row.keys === "metadata+empty-headers") headers = {};
  if (row.keys === "split") { metadata = new grpc.Metadata(); metadata.set("x-api-key", KEYS["x-api-key"]); headers = { "x-secret-key": KEYS["x-secret-key"] }; }
  return { options: {}, metadata, headers };
}

describe("the one decider, over the table", () => {
  test.each([...ROWS, ...DISPATCHER_ROWS])("$name", row => {
    const { metadata, headers } = setup(row);
    let decision: ReturnType<typeof decideFiRequest> | undefined, error: any;
    try {
      decision = decideFiRequest({ target: row.target, transport: row.transport, headers, metadata, allowInsecureHttp: row.optIn === "option" ? true : undefined,
        envRoute: row.fiEnv ? row.target : undefined, ...(row.dispatcher ? { dispatcher: new DISPATCHERS[row.dispatcher]() } : {}) });
    } catch (e) { error = e; }
    if (row.expect.decision === "refuse") {
      expect(error?.code).toBe(row.expect.code);
      expect(String(error?.message)).not.toMatch(/synthetic|collector\.example|evil\.example|futureagi/);
      return;
    }
    expect(error).toBeUndefined();
    expect(decision!.dialedHost).toBe(row.expect.host);
    expect(decision!.tls).toBe(row.expect.tls);
    expect(decision!.keys).toBe(row.expect.keysSent);
  });
});

describe("the same table, end to end through each transport", () => {
  test.each(ROWS)("$name", row => {
    const { metadata, headers } = setup(row);
    if (row.fiEnv) { if (row.transport === "grpc") process.env.FI_GRPC_COLLECTOR_ENDPOINT = row.target; else process.env.FI_BASE_URL = row.target; }
    const options: Record<string, unknown> = { ...(row.fiEnv ? {} : { endpoint: row.target }), ...(headers ? { headers } : {}), ...(metadata ? { metadata } : {}),
      ...(row.optIn === "option" ? { allowInsecureHttp: true } : {}), transport: row.transport === "grpc" ? Transport.GRPC : Transport.HTTP };
    let exporter: unknown, error: any;
    try { exporter = createSpanExporter(options as any); } catch (e) { error = e; }
    if (row.expect.decision === "refuse") {
      expect(error).toBeDefined();
      expect(error.code).toBe(row.expect.code);
      return;
    }
    expect(error).toBeUndefined();
    const sent = outgoing(exporter, row.transport);
    expect(sent.host).toBe(row.expect.host);
    expect(sent.tls).toBe(row.expect.tls);
    expect(sentKeys(sent.headers)).toBe(row.expect.keysSent);
    never(sent.headers);
    if (row.keys === "env") expect(sent.headers["x-api-key"]).toBe(ENV_KEYS.FI_API_KEY);
  });
});

describe("a refused target sends no key anywhere", () => {
  test("grpc://grpc.futureagi.com\\@evil.example:4317 with the environment's keys builds nothing", () => {
    Object.assign(process.env, ENV_KEYS);
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: "grpc://grpc.futureagi.com\\@evil.example:4317" } as any)).toThrow(expect.objectContaining({ code: "FI_HOST_MISMATCH" }));
    expect(() => createSpanExporter({ transport: Transport.GRPC, url: "grpc://grpc.futureagi.com\\@evil.example:4317" } as any)).toThrow();
  });
});

describe("allowInsecureHttp reaches every constructor path", () => {
  test("register on HTTP", () => {
    const provider = register({ projectName: "p", endpoint: "http://collector.example:4318", headers: { ...KEYS }, allowInsecureHttp: true, setGlobalTracerProvider: false });
    expect(provider).toBeDefined();
    expect(() => register({ projectName: "p", endpoint: "http://collector.example:4318", headers: { ...KEYS }, setGlobalTracerProvider: false })).toThrow(/FI_ALLOW_INSECURE_HTTP/);
  });
  test("FITracerProvider on HTTP and gRPC", () => {
    expect(new FITracerProvider({ endpoint: "http://collector.example:4318", headers: { ...KEYS }, allowInsecureHttp: true })).toBeDefined();
    expect(new FITracerProvider({ transport: Transport.GRPC, endpoint: "http://collector.example:4317", headers: { ...KEYS }, allowInsecureHttp: true })).toBeDefined();
    expect(() => new FITracerProvider({ endpoint: "http://collector.example:4318", headers: { ...KEYS } })).toThrow(/FI_ALLOW_INSECURE_HTTP/);
  });
  test("the one warning is also emitted through process.emitWarning", () => {
    jest.isolateModules(() => {
      const emitted: string[] = [];
      const spy = jest.spyOn(process, "emitWarning").mockImplementation(((message: unknown) => { emitted.push(String(message)); }) as any);
      try {
        const m = require("../otel");
        m.createSpanExporter({ endpoint: "http://collector.example:4318", headers: { ...KEYS }, allowInsecureHttp: true });
        m.createSpanExporter({ endpoint: "http://other.example:4318", headers: { ...KEYS }, allowInsecureHttp: true });
      } finally { spy.mockRestore(); }
      expect(emitted).toHaveLength(1);
      expect(emitted[0]).toMatch(/FI_ALLOW_INSECURE_HTTP/);
      expect(emitted[0]).not.toMatch(/synthetic/);
    });
  });
});

describe("a case-variant duplicate key header is judged by its non-empty entry", () => {
  test("{'X-Api-Key': '', 'x-api-key': 'k', ...} to plaintext is refused like any pair", () => {
    const headers = { "X-Api-Key": "", "x-api-key": KEYS["x-api-key"], "x-secret-key": KEYS["x-secret-key"] };
    expect(() => createSpanExporter({ endpoint: "http://collector.example:4318", headers } as any)).toThrow(/FI_ALLOW_INSECURE_HTTP/);
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: "http://collector.example:4317", headers } as any)).toThrow(/FI_ALLOW_INSECURE_HTTP/);
  });
  test("a non-empty entry in a different case than the other key", () => {
    const headers = { "X-API-KEY": KEYS["x-api-key"], "X-Secret-Key": "", "x-secret-key": KEYS["x-secret-key"] };
    expect(() => createSpanExporter({ endpoint: "http://collector.example:4318", headers } as any)).toThrow(/FI_ALLOW_INSECURE_HTTP/);
  });
});

describe("the eval checks route through the decider", () => {
  const tag = () => ({ type: EvalTagType.OBSERVATION_SPAN, value: EvalSpanKind.LLM, eval_name: EvalName.CHUNK_ATTRIBUTION, config: {}, custom_eval_name: "Chunk_Attribution",
    mapping: { context: "raw.input", output: "raw.output" }, model: ModelChoices.TURING_SMALL, toDict: () => ({ eval_name: EvalName.CHUNK_ATTRIBUTION, custom_eval_name: "Chunk_Attribution" }) });
  test("an ambiguous base URL sends nothing", async () => {
    const fetchSpy = jest.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({ result: { exists: false } }), text: async () => "" } as Response);
    try {
      Object.assign(process.env, ENV_KEYS);
      for (const base of ["https://api.futureagi.com%5C@collector.example", "http://collector.example/x@api.futureagi.com", "https:///api.futureagi.com"]) {
        process.env.FI_BASE_URL = base;
        await checkCustomEvalConfigExists("p", [tag()]);
      }
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally { fetchSpy.mockRestore(); }
  });
});

describe("NO_PROXY matching follows the standard library's proxy_bypass_environment", () => {
  test.each([
    ["collector.example", "collector.example", "4318", true], ["a.example.com", ".example.com", "80", true], ["example.com", ".example.com", "80", true],
    ["a.example.com", "example.com", "80", true], ["notexample.com", "example.com", "80", false], ["x.test", "*", "80", true],
    ["collector.example", "collector.example:4318", "4318", true], ["collector.example", "collector.example:9999", "4318", false],
    ["collector.example", " Collector.EXAMPLE ", "4318", true], ["collector.example", ",,", "4318", false], ["10.0.0.5", "10.0.0.0/8", "80", false],
  ])("%s against %p (port %s) -> bypassed %p", (host, noProxy, port, bypassed) => {
    expect(matchesNoProxy(host, port, noProxy)).toBe(bypassed);
  });
});
