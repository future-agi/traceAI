import { execFileSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { afterAll, beforeAll, expect, it } from "vitest";

const root = resolve(__dirname, "../..");
const localRequire = createRequire(join(root, "package.json"));
let scratch: string;
// Descendants can resolve the same external peers as the installed integration.
beforeAll(() => { scratch = mkdtempSync(join(root, ".converter-test-")); });
afterAll(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

it("exports the installed converter's GenAI schema from a fully bundled ESM entry", () => {
  // Resolve Vitest -> Vite -> esbuild through their own dependency scopes (pnpm).
  const vite = createRequire(localRequire.resolve("vitest/package.json")).resolve("vite/package.json");
  createRequire(vite)("esbuild").buildSync({
    entryPoints: [join(root, "dist/esm/index.js")],
    outfile: join(scratch, "entry.mjs"),
    bundle: true, packages: "external", platform: "node", format: "esm",
  });

  // Use native ESM, without Vitest's transforms or a CommonJS require shim.
  // Intercept only the final transport; the runtime, converter and batching are real.
  writeFileSync(join(scratch, "probe.mjs"), `
    import assert from 'node:assert/strict';
    import { createRequire } from 'node:module';
    import { createFIObservability } from './entry.mjs';
    import { SpanConverter } from '@mastra/otel-exporter';
    import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';

    const urls = [];
    OTLPTraceExporter.prototype.export = (spans, callback) => {
      urls.push(...spans.map(span => span.instrumentationScope.schemaUrl));
      callback({ code: 0 });
    };
    const obs = createFIObservability({ serviceName: 'schema-test', apiKey: 'a', secretKey: 'b',
      endpoint: 'http://127.0.0.1:1/traces' });
    try {
      const instance = obs.getDefaultInstance();
      const span = instance.startSpan({ type: 'generic', name: 'schema' });
      span.end();
      await new Promise(resolve => setImmediate(resolve));
      await instance.flush();

      const version = createRequire(import.meta.url)('@mastra/otel-exporter/package.json').version;
      const latest = 'https://opentelemetry.io/schemas/1.41.0';
      // 1.38 converters emit no scope URL. Pin observed results for the regression
      // versions, not a second copy of production's version comparison.
      const known = { '1.0.0': undefined, '1.2.0': undefined, '1.6.0-alpha.2': latest, '1.6.0': latest };
      // For other releases, ask the real peer directly: the old converter ignores
      // format, while the newer one emits the 1.41 URL only when requested.
      const expected = Object.hasOwn(known, version) ? known[version] :
        (await new SpanConverter({ packageName: '@traceai/mastra', format: 'GenAI_v1_41_0' })
          .convertSpan(span.exportSpan(false))).instrumentationScope.schemaUrl;
      assert.deepEqual(urls, [expected], 'schema on the exported span for converter ' + version);
    } finally { await obs.shutdown(); }
  `);
  execFileSync(process.execPath, [join(scratch, "probe.mjs")], { timeout: 30_000 });
}, 40_000);

const floor = "GenAI_v1_38_0";
const latest = "GenAI_v1_41_0";
const version = (v: unknown) => JSON.stringify({ version: v });
const cases: [string, string | undefined, string][] = [
  ["absent manifest file", undefined, floor],
  ["blocked manifest export", version("1.6.0"), floor],
  ["invalid JSON", "{broken", floor],
  ["null manifest", "null", floor],
  ["missing version", "{}", floor],
  ["numeric version", version(1.6), floor],
  ...["1.6", ".6.0", "01.6.0", "1.06.0", "1.6.00", "1.6.0.0", "1.6.0junk", "1.6.0-",
    "1.6.0-alpha..2", "1.6.0-alpha.02", "1.6.0+", "1.6.0\n"].map(
    (v): [string, string, string] => [`invalid version ${JSON.stringify(v)}`, version(v), floor],
  ),
  ["1.0.0", version("1.0.0"), floor],
  ["1.5.99", version("1.5.99"), floor],
  ["1.6.0-alpha.2", version("1.6.0-alpha.2"), latest],
  ["1.6.0", version("1.6.0"), latest],
  ["1.6.0 with build metadata", version("1.6.0+build.007"), latest],
  ["2.0.0", version("2.0.0"), latest],
];

it.each(cases)("selects the converter format for %s", async (label, metadata, expected) => {
  // A fresh consumer per case isolates both Node's package resolution and module
  // caches. Never mutate the workspace's real converter or its pnpm store.
  const consumer = mkdtempSync(join(scratch, "consumer-"));
  copyFileSync(join(root, "package.json"), join(consumer, "package.json"));
  cpSync(join(root, "dist/src"), join(consumer, "dist/src"), { recursive: true });
  const peer = join(consumer, "node_modules/@mastra/otel-exporter");
  mkdirSync(peer, { recursive: true });
  const exports: Record<string, string> = { ".": "./index.cjs" };
  if (label !== "blocked manifest export") exports["./package.json"] = "./metadata.json";
  writeFileSync(join(peer, "package.json"), JSON.stringify({ name: "@mastra/otel-exporter", exports }));
  if (metadata !== undefined) writeFileSync(join(peer, "metadata.json"), metadata);
  writeFileSync(join(peer, "index.cjs"), `
    exports.SpanConverter = class {
      constructor({ format }) { exports.selected = format; }
    };
  `);
  const req = createRequire(join(consumer, "package.json"));
  const exporter = req("./dist/src/index.js").createFIMastraExporter({ apiKey: "a", secretKey: "b" });
  try { expect(req("@mastra/otel-exporter").selected).toBe(expected); }
  finally { await exporter.shutdown(); }
});
