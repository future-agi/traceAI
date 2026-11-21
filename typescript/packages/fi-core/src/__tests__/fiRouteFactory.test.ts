import { FI_ROUTE_FACTORY } from "../fi-route";

// The route factory is reachable by any code in the process, so it must never fill Future AGI keys from the
// environment: it exports only with a complete explicit key pair.
const ENV = { FI_API_KEY: "env-fi-key-synthetic", FI_SECRET_KEY: "env-fi-secret-synthetic" };
const PAIR = { "x-api-key": "explicit-key-synthetic", "x-secret-key": "explicit-secret-synthetic" };
const saved: Record<string, string | undefined> = {};
beforeEach(() => { for (const k of Object.keys(ENV)) { saved[k] = process.env[k]; } Object.assign(process.env, ENV); });
afterEach(() => { for (const k of Object.keys(ENV)) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

const factory = () => (globalThis as any)[FI_ROUTE_FACTORY] as (options: Record<string, unknown>) => { shutdown(): Promise<void> };

describe("the FI route factory", () => {
  test("its symbol name carries a protocol version", () => {
    expect(String(FI_ROUTE_FACTORY)).toMatch(/fi-route-factory\.v\d+\)?$/);
    expect(typeof factory()).toBe("function");
  });
  test.each([
    ["no headers", undefined],
    ["empty headers", {}],
    ["a key without a secret", { "x-api-key": "only-key" }],
    ["a secret without a key", { "x-secret-key": "only-secret" }],
    ["blank values", { "x-api-key": " ", "x-secret-key": "" }],
    ["unrelated headers", { authorization: "Bearer other" }],
  ])("refuses %s, whatever the endpoint, without using the environment keys", (_label, headers) => {
    for (const endpoint of ["https://evil.example", "http://127.0.0.1:1", "https://api.futureagi.com"]) {
      let message = "";
      try { factory()({ endpoint, ...(headers === undefined ? {} : { headers }) }); } catch (error) { message = String((error as Error).message); }
      expect(message).not.toBe("");
      expect(message).not.toContain(ENV.FI_API_KEY);
      expect(message).not.toContain(ENV.FI_SECRET_KEY);
      expect(message).not.toMatch(/evil\.example|127\.0\.0\.1/);
    }
  });
  test("refuses gRPC options without an explicit pair too", () => {
    expect(() => factory()({ endpoint: "evil.example:443", transport: "grpc" })).toThrow();
  });
  test("builds an exporter from a complete explicit pair", async () => {
    const exporter = factory()({ endpoint: "http://127.0.0.1:1", headers: { ...PAIR } });
    expect(typeof (exporter as any).export).toBe("function");
    await exporter.shutdown();
  });
  test("accepts the pair in any header-name case", async () => {
    const exporter = factory()({ endpoint: "http://127.0.0.1:1", headers: { "X-Api-Key": "k", "X-Secret-Key": "s" } });
    await exporter.shutdown();
  });
});
