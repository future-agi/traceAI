import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";

/**
 * Loading the @traceai/auto agent bundle (the preload path) touches nothing of the process-wide undici slot: a getter the application put
 * there never runs, its descriptor is unchanged, an empty slot stays empty, and the application's own fetch runs on Node's own undici. The
 * bundle contains no copy of undici's global module.
 */
const root = resolve(__dirname, "..");
const agent = process.env.TRACEAI_TEST_AGENT || join(root, "dist/agent");
const fiCoreRoot = resolve(dirname(require.resolve("@traceai/fi-core")), "..", "..");
const fiCoreRequire = createRequire(join(fiCoreRoot, "package.json"));
const UNDICI_6 = dirname(fiCoreRequire.resolve("undici"));

const SLOT = `
  const http = require("node:http");
  const slot = Symbol.for("undici.globalDispatcher.1");
  const mode = process.env.MODE; let ran = 0;
  const getter = () => { ran++; return undefined; };
  const marker = { dispatch() { return true; } };
  if (mode === "getter") Object.defineProperty(globalThis, slot, { configurable: true, get: getter });
  if (mode === "data") Object.defineProperty(globalThis, slot, { configurable: true, writable: true, value: marker });
  const state = () => { const d = Object.getOwnPropertyDescriptor(globalThis, slot);
    return "getterRan=" + ran + " descriptor=" + (!d ? "absent" : d.get ? (d.get === getter ? "same-getter" : "other-getter") : d.value === marker ? "same-data" : "other-data"); };
  console.log("BEFORE " + state());
  require(process.env.PRELOAD);
  console.log("LOADED " + state());
  (async () => {
    await new Promise(done => setTimeout(done, 200));
    console.log("SETTLED " + state());
    if (mode === "empty") {
      const server = http.createServer((req, res) => res.end("ok"));
      await new Promise(done => server.listen(0, "127.0.0.1", done));
      const r = await fetch("http://127.0.0.1:" + server.address().port + "/x");
      console.log("APP_FETCH_STATUS " + r.status);
      const d = Object.getOwnPropertyDescriptor(globalThis, slot);
      console.log("APP_DISPATCHER_IS_BUNDLED_UNDICI " + Boolean(d && d.value instanceof require(process.env.UNDICI_6).Agent));
      server.close();
    }
    process.exit(0);
  })();
`;
const run = (mode: string): string => {
  const result = spawnSync(process.execPath, ["-e", SLOT], { encoding: "utf8", timeout: 60000,
    env: { PATH: process.env.PATH ?? "", MODE: mode, PRELOAD: join(agent, "preload.cjs"), UNDICI_6, FI_BASE_URL: "http://127.0.0.1:9", FI_API_KEY: "synthetic-fi-key-0001", FI_SECRET_KEY: "synthetic-fi-secret-0001" } });
  return result.stdout + result.stderr;
};

describe("the agent bundle leaves the undici global slot untouched", () => {
  test("RED a getter on the slot never runs and is still the slot's own getter after the preload loaded", () => {
    const output = run("getter");
    expect(output).toContain("BEFORE getterRan=0 descriptor=same-getter");
    expect(output).toContain("LOADED getterRan=0 descriptor=same-getter");
    expect(output).toContain("SETTLED getterRan=0 descriptor=same-getter");
  });
  test("RED a data property on the slot keeps its own value", () => {
    const output = run("data");
    expect(output).toContain("SETTLED getterRan=0 descriptor=same-data");
  });
  test("RED an empty slot stays absent after the preload loaded, and the application's own fetch runs on Node's undici", () => {
    const output = run("empty");
    expect(output).toContain("LOADED getterRan=0 descriptor=absent");
    expect(output).toContain("SETTLED getterRan=0 descriptor=absent");
    expect(output).toContain("APP_FETCH_STATUS 200");
    expect(output).toContain("APP_DISPATCHER_IS_BUNDLED_UNDICI false");
  });
  test("the bundle holds no undici global module and no write to the slot", () => {
    const { readFileSync } = require("node:fs");
    const text: string = readFileSync(join(agent, "runtime.cjs"), "utf8");
    expect(text).not.toContain("undici.globalDispatcher.1");
    expect(text).not.toContain("globalDispatcher");
  });
});
