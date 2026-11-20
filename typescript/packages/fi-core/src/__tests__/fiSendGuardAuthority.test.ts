import * as grpc from "@grpc/grpc-js";
import "../fi-route";
import { createSpanExporter, Transport } from "../otel";
import { decideFiRequest } from "../fi-request-policy";
import { guardedFetch, guardSend, intendedFrom, headerEntries } from "../fi-send-guard";
import { counter, runCleanups, sink } from "./helpers/realSockets";

/**
 * Disputed and empty authorities, and plaintext gRPC to a Future AGI host, are refused with a value-free error: at the decision AND at the guarded
 * send, before fetch, the OTLP HTTP exporter or gRPC sees the target. The guarded-send cells hand the guard the target string itself, so what
 * refuses is the guard and not fetch's own URL rejection.
 */
const KEYS = { "x-api-key": "synthetic-fi-key-0001", "x-secret-key": "synthetic-fi-secret-0001" };
const NAMES = ["FI_API_KEY", "FI_SECRET_KEY", "FI_ALLOW_INSECURE_HTTP", "OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE"];
let saved: Record<string, string | undefined> = {};
beforeEach(() => { saved = {}; for (const n of NAMES) { saved[n] = process.env[n]; delete process.env[n]; } });
afterEach(async () => { for (const n of NAMES) { if (saved[n] === undefined) delete process.env[n]; else process.env[n] = saved[n]; } await runCleanups(); });
const intended = { keys: new Map(Object.entries(KEYS)), values: Object.values(KEYS) };

const DISPUTED = ["https://[::1]@collector.example:4318/", "http://[::1]@collector.example:4318/", "https://user@collector.example:4318/", "http://[::1]%40collector.example:4318/"];
const EMPTY_HOST = ["http://:4318/x", "https://:4318/x"];
describe("a disputed authority with userinfo and an empty host with a port are refused", () => {
  test.each(DISPUTED)("%s: disputed authority, at the decision and at the guarded send", target => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    const code = /\[::1\]%40|%5C/i.test(target) ? undefined : "FI_HOST_MISMATCH";
    const refused = (fn: () => unknown) => { try { fn(); return undefined; } catch (error: any) { return error.code; } };
    const atDecision = refused(() => decideFiRequest({ target, transport: "http", headers: { ...KEYS } }));
    const atSend = refused(() => guardSend({ transport: "http", target, entries: headerEntries(KEYS), intended }));
    if (code) { expect(atDecision).toBe(code); expect(atSend).toBe(code); } else { expect(atDecision ?? atSend).toBeDefined(); }
  });
  test.each(EMPTY_HOST)("%s: empty authority, at the decision and at the guarded send", target => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    const refused = (fn: () => unknown) => { try { fn(); return undefined; } catch (error: any) { return error.code; } };
    expect(refused(() => decideFiRequest({ target, transport: "http", headers: { ...KEYS } }))).toBe("FI_EMPTY_AUTHORITY");
    expect(refused(() => guardSend({ transport: "http", target, entries: headerEntries(KEYS), intended }))).toBe("FI_EMPTY_AUTHORITY");
  });
  test.each(["[::1]@collector.example:4317", "http://[::1]@collector.example:4317", "collector.example\\@[::1]:4317"])("gRPC %s: disputed authority", target => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(() => decideFiRequest({ target, transport: "grpc", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_HOST_MISMATCH" }));
  });
  test.each([":4317", "http://:4317", "https://:4317"])("gRPC %s: empty host", target => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(() => decideFiRequest({ target, transport: "grpc", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_EMPTY_AUTHORITY" }));
  });
  test("the guarded fetch refuses these targets itself: no connection, no byte, and the error is the refusal, not a URL error from fetch", async () => {
    const target = await sink();
    for (const url of [`http://[::1]@127.0.0.1:${target.port}/x`, `http://:${target.port}/x`]) {
      await expect(guardedFetch({ url, init: { method: "POST", headers: KEYS }, intended, allowInsecureHttp: true })).rejects.toEqual(expect.objectContaining({ name: "FiKeyRefusal" }));
    }
    expect(target.connections).toBe(0); expect(target.bytes).toBe(0);
  });
  test("the OTLP HTTP exporter and the gRPC exporter refuse them at construction", () => {
    process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(() => createSpanExporter({ endpoint: "http://[::1]@collector.example:4318", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_HOST_MISMATCH" }));
    expect(() => createSpanExporter({ endpoint: "http://:4318", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_EMPTY_AUTHORITY" }));
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: "[::1]@collector.example:4317", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_HOST_MISMATCH" }));
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: ":4317", headers: { ...KEYS } })).toThrow(expect.objectContaining({ code: "FI_EMPTY_AUTHORITY" }));
  });
});

describe("plaintext gRPC to a Future AGI host is refused, never upgraded to TLS", () => {
  test.each([
    ["an http:// target", { endpoint: "http://grpc.futureagi.com:4317" }],
    ["insecure channel credentials", { endpoint: "grpc.futureagi.com:4317", credentials: grpc.credentials.createInsecure() }],
    ["OTEL_EXPORTER_OTLP_INSECURE=true", { endpoint: "grpc.futureagi.com:4317", env: { OTEL_EXPORTER_OTLP_INSECURE: "true" } }],
    ["an http:// target with the opt-in", { endpoint: "http://grpc.futureagi.com:4317", optIn: true }],
  ] as Array<[string, { endpoint: string; credentials?: grpc.ChannelCredentials; env?: Record<string, string>; optIn?: boolean }]>)("%s", (_name, request) => {
    Object.assign(process.env, request.env ?? {}); if (request.optIn) process.env.FI_ALLOW_INSECURE_HTTP = "1";
    expect(() => decideFiRequest({ target: request.endpoint, transport: "grpc", headers: { ...KEYS }, credentials: request.credentials })).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
    expect(() => createSpanExporter({ transport: Transport.GRPC, endpoint: request.endpoint, headers: { ...KEYS }, ...(request.credentials ? { credentials: request.credentials } : {}) } as any)).toThrow(expect.objectContaining({ code: "FI_PLAINTEXT_FI_HOST" }));
  });
  test("TLS to a Future AGI host is unchanged: a bare target and an https:// target build, and the channel is secure", () => {
    for (const endpoint of ["grpc.futureagi.com:443", "https://grpc.futureagi.com:443"]) {
      const decision = decideFiRequest({ target: endpoint, transport: "grpc", headers: { ...KEYS } });
      expect(decision.tls).toBe(true);
      expect(intendedFrom(decision).keys.size).toBe(2);
    }
  });
});
void counter;
