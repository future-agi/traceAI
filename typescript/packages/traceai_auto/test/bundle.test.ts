import { cpSync, mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, symlinkSync, rmSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import { tmpdir } from "node:os";
import { resolve, join, dirname } from "node:path";
import { createServer, Server } from "node:http";
import { spawn, spawnSync } from "node:child_process";
import { traceAI as esbuildPlugin } from "../src/bundlers/esbuild";
import { traceAI as rollupPlugin } from "../src/bundlers/rollup";
import { TraceAIPlugin } from "../src/bundlers/webpack";
import type { AddressInfo } from "node:net";
const root = resolve(__dirname, ".."), base = process.env.TRACEAI_TEST_TMP || tmpdir();
let dir: string, agent: string, server: Server, url: string, spans: any[];
const app = `(async () => {
 const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify({model:'gpt-fixture',messages:[{role:'user',content:'hello'}]}) });
 console.log((await response.json()).choices[0].message.content);
})().catch(e => { console.error(e); process.exitCode = 1; });`;
const packageRequire = createRequire(join(root, "package.json"));
function withoutRegisterDeprecation(stderr: string): string {
  // Known upstream Node 26 warnings: module.register (tsx/IITM), punycode (OpenAI v4/node-fetch).
  return stderr.replace(/\(node:\d+\) \[(?:DEP0205|DEP0040)\][^\n]*\n(?:\(Use `node --trace-deprecation[^\n]*\n)?/g, "");
}
function link(name: string, appDir = dir) {
  // Some real packages intentionally do not export package.json.
  let target = dirname(packageRequire.resolve(name));
  while (true) {
    try { if (JSON.parse(readFileSync(join(target, "package.json"), "utf8")).name === name) break; } catch { /* Ascend to package root. */ }
    const parent = dirname(target); if (parent === target) throw new Error(`Cannot locate ${name}`); target = parent;
  }
  const path = join(appDir, "node_modules", name); mkdirSync(dirname(path), { recursive: true }); if (!existsSync(path)) symlinkSync(target, path, "dir");
}
function sdkApp(ext: string, appDir = dir): string {
  mkdirSync(appDir, { recursive: true }); link("openai", appDir);
  const file = join(appDir, `default-sdk.${ext}`);
  writeFileSync(file, `${ext === "mjs" ? "import OpenAI from 'openai';" : "const OpenAI = require('openai');"}\n(async () => { const client = new OpenAI({apiKey:'mock',baseURL:process.env.MOCK_URL+'/v1'}); console.log((await client.chat.completions.create({model:'gpt-fixture',messages:[{role:'user',content:'hello'}]})).choices[0].message.content); })();`);
  return file;
}
function run(args: string[], extra: Record<string, string> = {}, appDir = dir): Promise<{ stdout: string; stderr: string }> {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/^(FI_|OTEL_)/.test(key) || key === "NODE_OPTIONS") delete env[key];
  Object.assign(env, { FI_API_KEY: "fixture-key", FI_SECRET_KEY: "fixture-secret", FI_BASE_URL: url, MOCK_URL: url, FI_INSTRUMENTATIONS: "wire" }, extra);
  return new Promise((done, reject) => {
    const child = spawn(process.execPath, args, { cwd: appDir, env }); let stdout = "", stderr = "";
    child.stdout.on("data", data => { stdout += data; }); child.stderr.on("data", data => { stderr += data; });
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error(`Child timed out: ${stderr}`)); }, 15000);
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.on("close", code => { clearTimeout(timer); code === 0 ? done({ stdout, stderr }) : reject(new Error(`Child ${code}: ${stderr}\n${stdout}`)); });
  });
}
beforeAll(async () => {
  mkdirSync(base, { recursive: true }); dir = mkdtempSync(join(base, "traceai-standalone-"));
  agent = join(dir, "agent with spaces", "preload.cjs"); cpSync(process.env.TRACEAI_TEST_AGENT || join(root, "dist/agent"), dirname(agent), { recursive: true });
  for (const ext of ["cjs", "mjs", "ts"]) writeFileSync(join(dir, `app.${ext}`), app);
  server = createServer((req, res) => { const chunks: Buffer[] = []; req.on("data", c => chunks.push(c)); req.on("end", () => {
    if (req.url === "/tracer/v1/traces") spans.push(...JSON.parse(Buffer.concat(chunks).toString()).resourceSpans.flatMap((r: any) => r.scopeSpans.flatMap((s: any) => s.spans)));
    res.setHeader("content-type", "application/json"); res.end(req.url === "/tracer/v1/traces" ? "{}" : JSON.stringify({ model: "gpt-fixture", choices: [{ message: { role: "assistant", content: "hello" } }] }));
  }); });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done)); url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); rmSync(dir, { recursive: true, force: true }); });
beforeEach(() => { spans = []; });
test.each(["cjs", "mjs"])("relocated standalone --require traces %s without any installed dependencies", async ext => {
  expect(existsSync(join(dir, "node_modules", "@traceai", "auto"))).toBe(false);
  const result = await run(["--require", agent, `app.${ext}`]);
  expect(result.stdout.trim()).toBe("hello"); expect(result.stderr).toBe(""); expect(spans).toHaveLength(1);
});
test("NODE_OPTIONS --require with a quoted path and duplicate injection is idempotent", async () => {
  const result = await run(["-r", agent, "app.mjs"], { NODE_OPTIONS: `--require ${JSON.stringify(agent)}` });
  expect(result.stderr).toBe(""); expect(spans).toHaveLength(1);
});
test("standalone preload works with TypeScript via the real tsx loader", async () => {
  const result = await run(["--require", agent, "--import", require.resolve("tsx"), "app.ts"]);
  expect(result.stdout.trim()).toBe("hello");
  // Node 26 deprecates the module.register API used by tsx itself.
  expect(withoutRegisterDeprecation(result.stderr)).toBe(""); expect(spans).toHaveLength(1);
});
test("missing module.register falls back to CJS/fetch without a bad Node option", async () => {
  writeFileSync(join(dir, "old-node.cjs"), "require('node:module').register = undefined;");
  const result = await run(["-r", join(dir, "old-node.cjs"), "-r", agent, "app.cjs"]);
  expect(result.stdout.trim()).toBe("hello"); expect(spans).toHaveLength(1); expect(result.stderr).not.toContain("bad option");
});
test("standalone bundle retains app-owned OTel SDK, context and exporter without global errors", async () => {
  for (const name of ["@opentelemetry/api", "@opentelemetry/sdk-trace-base", "@opentelemetry/context-async-hooks", "@opentelemetry/resources"]) link(name);
  writeFileSync(join(dir, "sdk.cjs"), `const { trace, context, diag } = require('@opentelemetry/api');
 const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
 const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
 const errors = []; diag.setLogger({error(...args){errors.push(args.join(' '))},warn(){},info(){},debug(){},verbose(){}});
 const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({spanProcessors:[new SimpleSpanProcessor(exporter)]});
 trace.setGlobalTracerProvider(provider); const manager = new AsyncLocalStorageContextManager().enable(); context.setGlobalContextManager(manager);
 process.on('exit', () => console.log(JSON.stringify({same:trace.getTracerProvider().getDelegate()===provider, contextSame:context._getContextManager()===manager, appSpans:exporter.getFinishedSpans().length, errors})));`);
  const result = await run(["-r", join(dir, "sdk.cjs"), "-r", agent, "app.mjs"]);
  expect(JSON.parse(result.stdout.trim().split("\n").at(-1)!)).toEqual({ same: true, contextSame: true, appSpans: 1, errors: [] });
  expect(result.stderr).toBe(""); expect(spans).toHaveLength(1);
});
test.each(["cjs", "mjs"])("standalone agent traces the real default OpenAI transport with only openai installed (%s)", async ext => {
  const isolated = join(dir, `only-openai-${ext}`), file = sdkApp(ext, isolated);
  expect(readdirSync(join(isolated, "node_modules"))).toEqual(["openai"]);
  const result = await run(["-r", agent, file], { FI_INSTRUMENTATIONS: "openai,wire" }, isolated);
  expect(result.stdout.trim()).toBe("hello"); expect(withoutRegisterDeprecation(result.stderr)).toBe(""); expect(spans).toHaveLength(1);
  expect(spans[0].attributes.some((a: any) => a.key === "traceai.capture.layer")).toBe(false);
});
test.each(["cjs", "mjs"])("mixed embedded OpenAI and installed Anthropic registries both preserve OpenAI capture (%s)", async ext => {
  const isolated = join(dir, `mixed-registries-${ext}`), file = sdkApp(ext, isolated);
  link("@traceai/anthropic", isolated); link("@anthropic-ai/sdk", isolated);
  expect(existsSync(join(isolated, "node_modules/@traceai/openai"))).toBe(false);
  const result = await run(["-r", agent, file], { FI_INSTRUMENTATIONS: "openai,anthropic,wire" }, isolated);
  expect(result.stdout.trim()).toBe("hello"); expect(withoutRegisterDeprecation(result.stderr)).toBe(""); expect(spans).toHaveLength(1);
  expect(spans[0].attributes.some((a: any) => a.key === "traceai.capture.layer")).toBe(false);
});
test("without module.register the embedded integration still patches the real CJS OpenAI SDK", async () => {
  writeFileSync(join(dir, "old-node.cjs"), "require('node:module').register = undefined;");
  const result = await run(["-r", join(dir, "old-node.cjs"), "-r", agent, sdkApp("cjs")], { FI_INSTRUMENTATIONS: "openai,wire" });
  expect(result.stdout.trim()).toBe("hello"); expect(result.stderr).toContain("ESM loader could not be installed"); expect(result.stderr).not.toContain("bad option");
  expect(spans).toHaveLength(1); expect(spans[0].attributes.some((a: any) => a.key === "traceai.capture.layer")).toBe(false);
});
test("embedded OpenAI integration also coexists with the application's global OTel SDK", async () => {
  const result = await run(["-r", join(dir, "sdk.cjs"), "-r", agent, sdkApp("cjs")], { FI_INSTRUMENTATIONS: "openai,wire" });
  expect(JSON.parse(result.stdout.trim().split("\n").at(-1)!)).toEqual({ same: true, contextSame: true, appSpans: 1, errors: [] });
  expect(withoutRegisterDeprecation(result.stderr)).toBe(""); expect(spans).toHaveLength(1);
});
test("unsupported Node and Node's internal loader thread never load the dependency graph", () => {
  const code = readFileSync(agent, "utf8");
  for (const version of ["8.0.0", "12.0.0", "16.20.0"]) {
    const require = jest.fn(); runInNewContext(code, { process: { versions: { node: version } }, require, console }); expect(require).not.toHaveBeenCalled();
  }
  const require = jest.fn(() => ({ isInternalThread: true }));
  runInNewContext(code, { process: { versions: { node: "26.0.0" } }, require, console }); expect(require).toHaveBeenCalledTimes(1);
});
test.each(["missing-package", "missing-bundle-input"])("launcher build rejects %s instead of silently omitting embedded instrumentation", async failure => {
  const script = readFileSync(join(root, "scripts/build-agent.cjs"), "utf8"), fakeProcess = { exitCode: 0 }, error = jest.fn();
  const build = jest.fn(async () => ({ metafile: { inputs: {} } }));
  const loader: any = (name: string) => name === "esbuild" ? { build } : name === "node:fs" ? { ...require("node:fs"), mkdirSync: jest.fn() } : packageRequire(name);
  loader.resolve = (name: string) => { if (name === "@traceai/openai" && failure === "missing-package") throw new Error("Required OpenAI build is missing"); return packageRequire.resolve(name); };
  runInNewContext(script, { require: loader, __dirname: join(root, "scripts"), console: { error, log() {} }, process: fakeProcess });
  await new Promise<void>(done => setImmediate(done));
  expect(fakeProcess.exitCode).toBe(1); expect(error).toHaveBeenCalled(); expect(build).toHaveBeenCalledTimes(failure === "missing-package" ? 0 : 1);
  expect(JSON.parse(readFileSync(join(root, "tsconfig.json"), "utf8")).references).toEqual(expect.arrayContaining([{ path: "../traceai_openai" }, { path: "../traceai_anthropic" }]));
});
test.each(["esbuild-cjs", "esbuild-esm", "rollup-cjs", "rollup-esm", "webpack-cjs", "webpack-esm", "vite-esm"])("real %s plugin-only build patches external default OpenAI before its dependency graph loads", async tool => {
  const appDir = join(dir, tool), entry = sdkApp("mjs", appDir), esm = tool.endsWith("esm"), output = join(appDir, `server.${esm ? "mjs" : "cjs"}`);
  const options = { agentPath: agent, server: true };
  if (tool.startsWith("esbuild")) await require("esbuild").build({ entryPoints: [entry], bundle: true, platform: "node", format: esm ? "esm" : "cjs", external: ["openai"], outfile: output, plugins: [esbuildPlugin(options), esbuildPlugin(options)] });
  else if (tool.startsWith("rollup")) {
    const build = await require("rollup").rollup({ input: entry, external: ["openai"], plugins: [rollupPlugin(options), rollupPlugin(options)] });
    try { await build.write({ file: output, format: esm ? "es" : "cjs" }); } finally { await build.close(); }
  } else if (tool.startsWith("webpack")) {
    const compiler = require("webpack")({ mode: "development", devtool: false, target: "node", entry, parallelism: 1, externals: { openai: esm ? "module openai" : "commonjs openai" }, experiments: { outputModule: esm }, output: { path: appDir, filename: `server.${esm ? "mjs" : "cjs"}`, module: esm }, plugins: [new TraceAIPlugin(options), new TraceAIPlugin(options)] });
    await new Promise<void>((done, reject) => compiler.run((error: any, stats: any) => compiler.close((close: any) => error || close || stats.hasErrors() ? reject(error || close || new Error(stats.toString())) : done())));
  } else {
    const script = join(appDir, "build-vite.mjs");
    writeFileSync(script, `import {build} from ${JSON.stringify(require.resolve("vite").replace("index.cjs", "index.js"))}; import plugin from ${JSON.stringify(join(root, "dist/src/bundlers/vite.js"))}; await build({configFile:false,root:${JSON.stringify(appDir)},logLevel:'silent',plugins:[plugin.traceAI(${JSON.stringify(options)}),plugin.traceAI(${JSON.stringify(options)})],ssr:{external:['openai']},build:{ssr:${JSON.stringify(entry)},outDir:${JSON.stringify(join(appDir, "out"))},rollupOptions:{output:{entryFileNames:'server.mjs'}}}});`);
    const built = spawnSync(process.execPath, [script], { cwd: appDir, encoding: "utf8", timeout: 20000 }); expect(built.error).toBeUndefined(); expect(built.status).toBe(0); expect(built.stderr).toBe("");
  }
  const result = await run([tool === "vite-esm" ? join(appDir, "out/server.mjs") : output], { FI_INSTRUMENTATIONS: "openai,wire" }, appDir);
  expect(result.stdout.trim()).toBe("hello"); expect(withoutRegisterDeprecation(result.stderr)).toBe(""); expect(spans).toHaveLength(1);
  expect(spans[0].attributes.some((a: any) => a.key === "traceai.capture.layer")).toBe(false);
});
test.each(["cjs", "mjs"])("project-installed dedicated OpenAI instrumentation is discovered by bundle (%s)", async ext => {
  link("openai");
  const instrumentor = join(dir, "node_modules", "@traceai", "openai"); mkdirSync(dirname(instrumentor), { recursive: true });
  if (!existsSync(instrumentor)) symlinkSync(dirname(dirname(dirname(require.resolve("@traceai/openai")))), instrumentor, "dir");
  writeFileSync(join(dir, `sdk-app.${ext}`), `${ext === "mjs" ? "import OpenAI from 'openai';" : "const OpenAI = require('openai');"}\n(async () => { const client = new OpenAI({apiKey:'mock',baseURL:process.env.MOCK_URL+'/v1',fetch:globalThis.fetch}); console.log((await client.chat.completions.create({model:'gpt-fixture',messages:[{role:'user',content:'hello'}]})).choices[0].message.content); })();`);
  const result = await run(["-r", agent, `sdk-app.${ext}`], { FI_INSTRUMENTATIONS: "openai,wire" });
  expect(result.stdout.trim()).toBe("hello"); expect(withoutRegisterDeprecation(result.stderr)).toBe(""); expect(spans).toHaveLength(1);
  const attr = spans[0].attributes.find((a: any) => ["fi.span.kind", "gen_ai.span.kind"].includes(a.key)); expect(attr.value.stringValue).toBe("LLM");
  expect(spans[0].attributes.some((a: any) => a.key === "traceai.capture.layer")).toBe(false);
});
