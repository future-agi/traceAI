import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, symlinkSync } from "node:fs";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { runInNewContext } from "node:vm";
import { traceAI as esbuildPlugin } from "../src/bundlers/esbuild";
import { traceAI as rollupPlugin } from "../src/bundlers/rollup";
import { traceAI as vitePlugin } from "../src/bundlers/vite";
import { TraceAIPlugin } from "../src/bundlers/webpack";
import { withTraceAI } from "../src/bundlers/next";
import { MARKER, prepend } from "../src/bundlers/common";
const esbuild = require("esbuild"), { rollup } = require("rollup"), webpack = require("webpack");
let dir: string, entry: string;
function occurrences(code: string): number { return code.split(MARKER).length - 1; }
function executes(file: string): void {
  const child = spawnSync(process.execPath, [file], { env: { ...process.env, OTEL_SDK_DISABLED: "true", NODE_OPTIONS: "" }, encoding: "utf8", timeout: 10000 });
  expect(child.error).toBeUndefined(); expect(child.status).toBe(0); expect(child.stderr).toBe(""); expect(child.stdout.trim()).toBe("app executed");
}
beforeAll(() => { mkdirSync("/Volumes/nikm4/tmp", { recursive: true }); dir = mkdtempSync("/Volumes/nikm4/tmp/traceai-plugins-"); entry = join(dir, "entry.js"); writeFileSync(entry, "console.log('app executed');\n"); });
afterAll(() => rmSync(dir, { recursive: true, force: true }));
test.each(["cjs", "esm"])("real esbuild %s emits one preload only in server entry points", async format => {
  const file = join(dir, format === "esm" ? "esbuild.mjs" : "esbuild.cjs");
  const output = await esbuild.build({ entryPoints: [entry], bundle: true, platform: "node", format, outfile: file, sourcemap: true, plugins: [esbuildPlugin(), esbuildPlugin()] });
  expect(occurrences(readFileSync(file, "utf8"))).toBe(1); executes(file);
  const baseline = await esbuild.build({ entryPoints: [entry], bundle: true, platform: "node", format, outfile: file, sourcemap: true, write: false });
  const mappings = JSON.parse(baseline.outputFiles.find((f: any) => f.path.endsWith(".map")).text).mappings;
  expect(JSON.parse(readFileSync(file + ".map", "utf8")).mappings).toBe((format === "cjs" ? ";" : "") + mappings);
});
test("real esbuild canonicalizes symlinked entry paths before matching its metafile", async () => {
  const alias = join(dir, "entry-alias.js"); symlinkSync(entry, alias);
  const file = join(dir, "symlink-entry.cjs");
  await esbuild.build({ entryPoints: [alias], bundle: true, platform: "node", format: "cjs", outfile: file, plugins: [esbuildPlugin()] });
  expect(occurrences(readFileSync(file, "utf8"))).toBe(1); executes(file);
});
test.each(["array", "advanced", "record", "index", "custom-extension", "symlink"])("real esbuild resolves %s extensionless entries and activates preload without touching dynamic chunks", async kind => {
  const source = join(dir, `extensionless-${kind}`); mkdirSync(join(source, "src"), { recursive: true });
  const agent = join(source, "agent.cjs"); writeFileSync(agent, "globalThis.traced = true;");
  writeFileSync(join(source, "package.json"), '{"type":"module"}');
  writeFileSync(join(source, "src/dynamic.ts"), "if (!globalThis.traced) throw new Error('late preload');");
  const input = kind === "index" ? "src/server/index.ts" : kind === "custom-extension" ? "src/server.custom" : "src/server.ts";
  if (kind === "index") mkdirSync(join(source, "src/server"));
  writeFileSync(join(source, input), `if (!globalThis.traced) throw new Error('missing preload'); console.log('app executed'); import(${JSON.stringify(kind === "index" ? "../dynamic" : "./dynamic")});`);
  if (kind === "symlink") symlinkSync(join(source, "src/server.ts"), join(source, "src/alias.ts"));
  const entry = kind === "symlink" ? "./src/alias" : "./src/server";
  const entryPoints = kind === "advanced" ? [{ in: entry, out: "server" }] : kind === "record" ? { server: entry } : [entry];
  const result = await esbuild.build({ absWorkingDir: source, entryPoints, ...(kind === "custom-extension" ? { resolveExtensions: [".custom", ".ts"], loader: { ".custom": "ts" } } : {}), bundle: true, splitting: true, platform: "node", format: "esm", outdir: "out", plugins: [esbuildPlugin({ agentPath: agent })] });
  const entries = Object.entries<any>(result.metafile.outputs).filter(([, output]) => output.entryPoint && !output.entryPoint.endsWith("dynamic.ts"));
  expect(entries).toHaveLength(1);
  const file = resolve(source, entries[0][0]); expect(occurrences(readFileSync(file, "utf8"))).toBe(1); executes(file);
  const dynamic = Object.keys(result.metafile.outputs).filter(name => /dynamic-.*\.js$/.test(name)); expect(dynamic).toHaveLength(1);
  expect(occurrences(readFileSync(resolve(source, dynamic[0]), "utf8"))).toBe(0);
});
test.each(["relative", "alias", "package"])("real esbuild %s CJS entry activates the built standalone agent", async kind => {
  const source = join(dir, `resolved-cjs-${kind}`); mkdirSync(join(source, "src"), { recursive: true });
  const code = "console.log(Boolean(globalThis[Symbol.for('traceai.auto.agent.v1')]?.active));";
  writeFileSync(join(source, "src/server.ts"), code);
  if (kind === "package") {
    mkdirSync(join(source, "node_modules/pkg"), { recursive: true });
    writeFileSync(join(source, "node_modules/pkg/package.json"), '{"name":"pkg","main":"server.js"}');
    writeFileSync(join(source, "node_modules/pkg/server.js"), code);
  }
  const file = join(source, "output.cjs");
  const input = kind === "relative" ? "./src/server" : kind === "alias" ? "entry-alias" : "pkg";
  await esbuild.build({ absWorkingDir: source, entryPoints: [input], ...(kind === "alias" ? { alias: { "entry-alias": "./src/server.ts" } } : {}), bundle: true, platform: "node", format: "cjs", outfile: file, plugins: [esbuildPlugin()] });
  expect(occurrences(readFileSync(file, "utf8"))).toBe(1);
  const child = spawnSync(process.execPath, [file], { env: { ...process.env, FI_API_KEY: "fixture-private-key", FI_SECRET_KEY: "fixture-private-secret", FI_INSTRUMENTATIONS: "wire", FI_WIRE_CAPTURE: "true", OTEL_SDK_DISABLED: "false", NODE_OPTIONS: "" }, encoding: "utf8", timeout: 10000 });
  expect(child.error).toBeUndefined(); expect(child.status).toBe(0); expect(child.stderr).toBe(""); expect(child.stdout.trim()).toBe("true");
});
test("round3 esbuild glob entries canonicalize each symlink match before injection", async () => {
  const source = join(dir, "symlink-glob"), real = join(dir, "real-glob"); mkdirSync(source); mkdirSync(real);
  writeFileSync(join(real, "server.js"), "console.log('app executed');"); symlinkSync(join(real, "server.js"), join(source, "server.js"));
  const output = join(dir, "symlink-glob-out");
  const result = await esbuild.build({ absWorkingDir: dir, entryPoints: ["symlink-glob/*.js"], bundle: true, platform: "node", format: "cjs", outdir: output, plugins: [esbuildPlugin()] });
  const entry = Object.keys(result.metafile.outputs).find(name => name.endsWith("server.js"))!;
  expect(occurrences(readFileSync(resolve(dir, entry), "utf8"))).toBe(1); executes(resolve(dir, entry));
});
test("round3 esbuild unbundled inferred ESM preloads before linking static dependencies", async () => {
  const source = join(dir, "inferred-esm"); mkdirSync(source); writeFileSync(join(source, "package.json"), '{"type":"module"}');
  const agent = join(source, "agent.cjs"); writeFileSync(agent, "globalThis.traced = true;");
  writeFileSync(join(source, "dependency.js"), "if (!globalThis.traced) throw new Error('preload was late');");
  writeFileSync(join(source, "entry.js"), "import './dependency.js'; console.log('app executed');");
  const file = join(source, "output.js");
  await esbuild.build({ entryPoints: [join(source, "entry.js")], bundle: false, platform: "node", outfile: file, plugins: [esbuildPlugin({ agentPath: agent })] });
  expect(readFileSync(file, "utf8")).toContain("await import("); executes(file);
});
test.each(["array", "advanced", "record", "recursive"])("real esbuild expanded %s globs preload process entries but not dynamic imports", async kind => {
  const source = join(dir, `glob-${kind}`); mkdirSync(join(source, "nested"), { recursive: true });
  writeFileSync(join(source, "server.js"), "import('./nested/dynamic.js').then(console.log);");
  writeFileSync(join(source, "nested/dynamic.js"), "console.log('dynamic');");
  if (kind === "recursive") writeFileSync(join(source, "nested/server.js"), "console.log('nested entry');");
  const pattern = `glob-${kind}/${kind === "recursive" ? "**/server.js" : "*.js"}`;
  const entryPoints = kind === "advanced" ? [{ in: pattern, out: "server" }] : kind === "record" ? { server: pattern } : [pattern];
  const result = await esbuild.build({ absWorkingDir: dir, entryPoints, bundle: true, splitting: true, platform: "node", format: "esm", outdir: `glob-out-${kind}`, write: false, plugins: [esbuildPlugin(), esbuildPlugin()] });
  const entryOutputs = result.outputFiles.filter((f: any) => f.path.endsWith("/server.js"));
  expect(entryOutputs).toHaveLength(kind === "recursive" ? 2 : 1);
  expect(entryOutputs.every((f: any) => occurrences(f.text) === 1)).toBe(true);
  expect(result.outputFiles.filter((f: any) => !entryOutputs.includes(f)).every((f: any) => occurrences(f.text) === 0)).toBe(true);
  expect(result.outputFiles.some((f: any) => /dynamic-/.test(f.path))).toBe(true);
});
test.each(["cjs", "esm"])("real esbuild in-memory %s output and maps work through symlinked working directories", async format => {
  const canonicalDir = join(dir, `canonical-${format}`), alias = join(dir, `alias-${format}`);
  mkdirSync(canonicalDir); symlinkSync(canonicalDir, alias, "dir");
  writeFileSync(join(canonicalDir, "server.js"), "console.log('app executed');");
  const settings = { absWorkingDir: alias, entryPoints: ["server.js"], bundle: true, platform: "node", format, outdir: "out", sourcemap: true, write: false };
  const baseline = await esbuild.build(settings);
  const result = await esbuild.build({ ...settings, plugins: [esbuildPlugin()] });
  const output = result.outputFiles.find((f: any) => f.path.endsWith("/server.js"));
  expect(output.path).toBe(join(canonicalDir, "out/server.js")); expect(occurrences(output.text)).toBe(1);
  const before = JSON.parse(baseline.outputFiles.find((f: any) => f.path.endsWith(".map")).text).mappings;
  expect(JSON.parse(result.outputFiles.find((f: any) => f.path.endsWith(".map")).text).mappings).toBe((format === "cjs" ? ";" : "") + before);
  if (format === "esm") expect(result.outputFiles.some((f: any) => f.path.includes(".traceai-app.mjs"))).toBe(true);
});
test.each(["cjs", "esm", "default"])("round5 unnamed in-memory esbuild %s preserves baseline execution and output paths", async format => {
  const agent = join(dir, `stdout-${format}-agent.cjs`); writeFileSync(agent, "globalThis.traced = true;");
  const settings = { entryPoints: [entry], bundle: true, platform: "node", write: false, metafile: true, ...(format === "default" ? {} : { format }), sourcemap: "inline" };
  const baseline = await esbuild.build(settings), result = await esbuild.build({ ...settings, plugins: [esbuildPlugin({ agentPath: agent })] });
  expect(result.outputFiles.map((file: any) => file.path)).toEqual(baseline.outputFiles.map((file: any) => file.path));
  expect(result.outputFiles[0].path).toBe("<stdout>");
  const before = jest.fn(), after = jest.fn();
  if (format === "esm") expect(result.outputFiles[0].text).toBe(baseline.outputFiles[0].text); // No sibling payload can be deployed.
  runInNewContext(baseline.outputFiles[0].text, { console: { log: before } });
  runInNewContext(result.outputFiles[0].text, { require, console: { log: after } });
  expect(after.mock.calls).toEqual(before.mock.calls);
  expect(occurrences(result.outputFiles[0].text)).toBe(format === "esm" ? 0 : 1);
});
test.each([false, true])("round5 unnamed stdin esbuild CJS output retains baseline execution (sourcefile %s)", async named => {
  const agent = join(dir, `stdin-${named}-agent.cjs`); writeFileSync(agent, "globalThis.traced = true;");
  const settings = { stdin: { contents: "console.log('app executed');", ...(named ? { sourcefile: "server.js" } : {}) }, platform: "node", format: "cjs", write: false };
  const baseline = await esbuild.build(settings), result = await esbuild.build({ ...settings, plugins: [esbuildPlugin({ agentPath: agent })] });
  expect(result.outputFiles[0].path).toBe(baseline.outputFiles[0].path); expect(occurrences(result.outputFiles[0].text)).toBe(1);
  const before = jest.fn(), after = jest.fn(); runInNewContext(baseline.outputFiles[0].text, { console: { log: before } });
  runInNewContext(result.outputFiles[0].text, { require, console: { log: after } }); expect(after.mock.calls).toEqual(before.mock.calls);
});
test.each(["cjs", "esm"])("round5 esbuild custom JS output extension supports %s preload", async format => {
  const agent = join(dir, `custom-output-${format}-agent.cjs`); writeFileSync(agent, "globalThis.traced = true;");
  const result = await esbuild.build({ entryPoints: [entry], bundle: true, platform: "node", format, outdir: join(dir, `custom-output-${format}`), outExtension: { ".js": ".server" }, write: false, plugins: [esbuildPlugin({ agentPath: agent })] });
  const output = result.outputFiles.find((file: any) => file.path.endsWith(".server"));
  expect(occurrences(output.text)).toBe(1);
  if (format === "esm") expect(result.outputFiles.some((file: any) => file.path.endsWith(".traceai-app.mjs"))).toBe(true);
  else { const log = jest.fn(); runInNewContext(output.text, { require, console: { log } }); expect(log).toHaveBeenCalledWith("app executed"); }
});
test("exported ESM entry points fail clearly rather than silently losing live exports", async () => {
  const file = join(dir, "exported.js"); writeFileSync(file, "export let value = 1;");
  await expect(esbuild.build({ entryPoints: [file], platform: "node", format: "esm", outfile: join(dir, "exported.mjs"), write: false, plugins: [esbuildPlugin()], logLevel: "silent" })).rejects.toThrow("external --require preload");
  const build = await rollup({ input: file, plugins: [rollupPlugin({ server: true })] });
  try { await expect(build.generate({ format: "es" })).rejects.toThrow("external --require preload"); } finally { await build.close(); }
});
test("real esbuild browser build has no Node agent and in-memory builds work", async () => {
  const browser = await esbuild.build({ entryPoints: [entry], bundle: true, platform: "browser", write: false, plugins: [esbuildPlugin()] });
  expect(occurrences(browser.outputFiles[0].text)).toBe(0); expect(browser.outputFiles[0].text).not.toContain("preload.cjs");
  const node = await esbuild.build({ entryPoints: [entry], bundle: true, platform: "node", format: "esm", outfile: join(dir, "memory.mjs"), write: false, plugins: [esbuildPlugin()] });
  expect(occurrences(node.outputFiles[0].text)).toBe(1);
});
test("real esbuild split chunks receive registration only on entry", async () => {
  writeFileSync(join(dir, "dep.js"), "export const value = 1;");
  writeFileSync(join(dir, "split.js"), "import('./dep.js').then(console.log);");
  const result = await esbuild.build({ entryPoints: [join(dir, "split.js")], bundle: true, splitting: true, platform: "node", format: "esm", outdir: join(dir, "split"), write: false, plugins: [esbuildPlugin()] });
  expect(result.outputFiles.filter((f: any) => f.path.endsWith("split.js")).every((f: any) => occurrences(f.text) === 1)).toBe(true);
  expect(result.outputFiles.filter((f: any) => !f.path.endsWith("split.js")).every((f: any) => occurrences(f.text) === 0)).toBe(true);
});
test.each(["cjs", "es"])("real Rollup %s output is idempotent and executable", async format => {
  const file = join(dir, format === "es" ? "rollup.mjs" : "rollup.cjs");
  const build = await rollup({ input: entry, plugins: [rollupPlugin({ server: true }), rollupPlugin({ server: true })] });
  try { await build.write({ file, format, sourcemap: true }); } finally { await build.close(); }
  expect(occurrences(readFileSync(file, "utf8"))).toBe(1); executes(file);
});
test("real Rollup browser build stays untouched and ambiguous targets fail clearly", async () => {
  expect(() => rollupPlugin()).toThrow("server: true");
  const build = await rollup({ input: entry, plugins: [rollupPlugin({ server: false })] });
  try { const result = await build.generate({ format: "es" }); expect(occurrences(result.output[0].code)).toBe(0); } finally { await build.close(); }
});
test("real Vite server and browser builds are separated", async () => {
  // Import Vite outside Jest's ESM VM while retaining a real production build.
  const script = join(dir, "vite-test.mjs"), output = join(dir, "vite-server");
  writeFileSync(script, `import { build } from ${JSON.stringify(require.resolve("vite").replace("index.cjs", "index.js"))};\nimport plugin from ${JSON.stringify(resolve(__dirname, "../dist/src/bundlers/vite.js"))};\nawait build({configFile:false,root:${JSON.stringify(dir)},logLevel:'silent',plugins:[plugin.traceAI(),plugin.traceAI()],build:{ssr:${JSON.stringify(entry)},outDir:${JSON.stringify(output)},minify:false}});\nawait build({configFile:false,root:${JSON.stringify(dir)},logLevel:'silent',plugins:[plugin.traceAI()],build:{outDir:${JSON.stringify(join(dir, "vite-client"))},minify:false,rollupOptions:{input:${JSON.stringify(entry)},output:{entryFileNames:'entry.js'}}}});`);
  const child = spawnSync(process.execPath, [script], { cwd: dir, encoding: "utf8", timeout: 20000 });
  expect(child.error).toBeUndefined(); expect(child.status).toBe(0);
  // Vite's final esbuild transform strips comments, so count the actual import, not its marker.
  expect(child.stderr).toBe(""); const serverCode = readFileSync(join(output, "entry.mjs"), "utf8"); expect(serverCode.split("preload.cjs")).toHaveLength(2); executes(join(output, "entry.mjs"));
  expect(readFileSync(join(dir, "vite-client/entry.js"), "utf8")).not.toContain("preload.cjs");
});
function webpackBuild(config: any): Promise<void> { return new Promise((done, reject) => {
  const compiler = webpack(config); compiler.run((error: any, stats: any) => compiler.close((closeError: any) => {
    if (error || closeError || stats.hasErrors()) reject(error || closeError || new Error(stats.toString())); else done();
  }));
}); }
test.each(["node", "web"])("real webpack %s target never leaks into browser output", async target => {
  const output = join(dir, `webpack-${target}`);
  await webpackBuild({ mode: "development", devtool: false, target, entry, output: { path: output, filename: "main.cjs" }, plugins: [new TraceAIPlugin(), new TraceAIPlugin()], parallelism: 1 });
  expect(occurrences(readFileSync(join(output, "main.cjs"), "utf8"))).toBe(target === "node" ? 1 : 0);
  if (target === "node") executes(join(output, "main.cjs"));
});
test("Next wrapper preserves callbacks, excludes edge/client, is idempotent and its generated webpack config builds", async () => {
  const original = jest.fn((config: any) => config), config = withTraceAI({ webpack: original, poweredByHeader: false });
  expect(withTraceAI(config)).toBe(config); expect(config.poweredByHeader).toBe(false);
  for (const context of [{ isServer: false }, { isServer: true, nextRuntime: "edge" }]) expect(config.webpack({ plugins: [] }, context).plugins).toHaveLength(0);
  const node = config.webpack({ mode: "development", target: "node", entry, parallelism: 1, output: { path: join(dir, "next"), filename: "main.cjs" } }, { isServer: true, nextRuntime: "nodejs" });
  expect(node.plugins).toHaveLength(1); expect(original).toHaveBeenCalledTimes(3);
  await webpackBuild(node); expect(occurrences(readFileSync(join(dir, "next/main.cjs"), "utf8"))).toBe(1); executes(join(dir, "next/main.cjs"));
  expect(withTraceAI(() => ({ a: 1 }))().a).toBe(1); expect((await withTraceAI(async () => ({ b: 2 }))()).b).toBe(2);
});
test.each(["nodejs", "edge"])("documented Next instrumentation register() is runtime-guarded and idempotent (%s)", runtime => {
  const file = join(dir, `instrumentation-${runtime}.mjs`);
  writeFileSync(file, `export async function register() { if (process.env.NEXT_RUNTIME === 'nodejs') await import(${JSON.stringify(resolve(__dirname, "../dist/esm/register.js"))}); }\nawait register(); await register(); console.log(Boolean(globalThis[Symbol.for('traceai.auto.agent.v1')]));`);
  const child = spawnSync(process.execPath, [file], { env: { ...process.env, NEXT_RUNTIME: runtime, OTEL_SDK_DISABLED: "true", NODE_OPTIONS: "" }, encoding: "utf8", timeout: 10000 });
  expect(child.error).toBeUndefined(); expect(child.status).toBe(0); expect(child.stderr).toBe(""); expect(child.stdout.trim()).toBe(String(runtime === "nodejs"));
});
test("Vite edge rendering is excluded; shebang prepend is idempotent", () => {
  const p = vitePlugin({ server: true }); p.configResolved({ build: { ssr: true }, ssr: { target: "webworker" } });
  expect(p.renderChunk("hello", { isEntry: true }, { format: "es" })).toBeNull();
  const code = "#!/usr/bin/env node\nconsole.log(1)"; const result = prepend(code, MARKER + "\n");
  expect(result.startsWith("#!/usr/bin/env node\n" + MARKER)).toBe(true); expect(prepend(result, MARKER)).toBe(result);
});
test.each(['"use strict";\n', '#!/usr/bin/env node\n/* comment */\n"custom";\n\'use strict\';\n', '', '"not a directive" + "expression";\n"use strict";\n'])("prepending preserves strict/non-strict semantics (%s)", prologue => {
  const code = prologue + "function mode(){return this === undefined;} answer(mode());", before = jest.fn(), after = jest.fn();
  runInNewContext(code, { answer: before }); runInNewContext(prepend(code, MARKER + "void 0;\n"), { answer: after });
  expect(after.mock.calls).toEqual(before.mock.calls);
});
test.each(["esbuild-cjs", "esbuild-esm", "rollup-cjs", "rollup-esm", "webpack-cjs", "webpack-esm"])("%s runtime preload failure does not break the app", async tool => {
  const agent = join(dir, tool + "-broken-agent.cjs"), file = join(dir, tool + "-fail-open." + (tool.endsWith("esm") ? "mjs" : "cjs"));
  writeFileSync(agent, "throw new Error('broken preload');"); const options = { agentPath: agent, server: true }, esm = tool.endsWith("esm");
  if (tool.startsWith("esbuild")) await esbuild.build({ entryPoints: [entry], platform: "node", format: esm ? "esm" : "cjs", outfile: file, plugins: [esbuildPlugin(options)] });
  else if (tool.startsWith("rollup")) {
    const build = await rollup({ input: entry, plugins: [rollupPlugin(options)] });
    try { await build.write({ file, format: esm ? "es" : "cjs" }); } finally { await build.close(); }
  } else await webpackBuild({ mode: "development", devtool: false, target: "node", entry, parallelism: 1, experiments: { outputModule: esm }, output: { path: dir, filename: file.slice(dir.length + 1), module: esm }, plugins: [new TraceAIPlugin(options)] });
  if (tool.startsWith("esbuild")) rmSync(agent); // Missing deployed preload, not just a module that throws.
  const child = spawnSync(process.execPath, [file], { env: { ...process.env, NODE_OPTIONS: "" }, encoding: "utf8", timeout: 10000 });
  expect(child.error).toBeUndefined(); expect(child.status).toBe(0); expect(child.stdout.trim()).toBe("app executed");
  expect(child.stderr.trim()).toBe("[traceai:auto] Preload failed; the application will continue.");
});
test("misconfigured platform and missing agent produce clear build errors", async () => {
  await expect(esbuild.build({ entryPoints: [entry], platform: "browser", write: false, plugins: [esbuildPlugin({ server: true })], logLevel: "silent" })).rejects.toThrow("browser/edge");
  await expect(esbuild.build({ entryPoints: [entry], platform: "node", write: false, plugins: [esbuildPlugin({ agentPath: join(dir, "absent.cjs") })], logLevel: "silent" })).rejects.toThrow("Agent preload not found");
});
