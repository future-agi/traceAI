// Child process for fiRoute.test.ts: a route factory that reports being called, then init() with whitespace-only keys.
Object.defineProperty(globalThis, Symbol.for("traceai.fi-core.fi-route-factory.v1"), {
  value: () => { console.log("ROUTE_BUILT"); throw new Error("the Future AGI route was built"); },
  enumerable: false, writable: false, configurable: false,
});
(async () => {
  const auto = await import("../../src");
  auto.init({ instrumentations: [], wireCapture: false } as any);
  console.log("INIT_DONE");
  await auto.shutdown();
})().catch(e => { console.error(String(e)); process.exitCode = 1; });
