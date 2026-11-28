// Child process for fiRoute.test.ts: a process-wide route factory that is not a function, then init() with credentials.
Object.defineProperty(globalThis, Symbol.for("traceai.fi-core.fi-route-factory.v1"), { value: "not a factory", enumerable: false, writable: false, configurable: false });
(async () => {
  const auto = await import("../../src");
  auto.init({ instrumentations: [], wireCapture: false } as any);
  await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
