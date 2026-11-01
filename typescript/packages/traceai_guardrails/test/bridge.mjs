// Run against an installed tarball: node test/bridge.mjs /absolute/consumer/path
// The consumer needs @guardrails-ai/core, the packed @traceai/guardrails, and
// @opentelemetry/{sdk-trace-node,exporter-trace-otlp-http,exporter-trace-otlp-proto,otlp-transformer}@0.57.2
// (sdk-trace-node uses 1.30.1). PYTHON_BIN must point at guardrails-ai==0.4.2.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { resolve, dirname } from "node:path";
import { createServer } from "node:http";
import { gunzipSync } from "node:zlib";
import { readFile, writeFile, unlink } from "node:fs/promises";
import { spawn } from "node:child_process";

const consumer = resolve(process.argv[2]);
const require = createRequire(resolve(consumer, "package.json"));
const { GuardrailsInstrumentation, isPatched } = require("@traceai/guardrails");
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");
const { SimpleSpanProcessor } = require("@opentelemetry/sdk-trace-base");
const { context, trace } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { OTLPTraceExporter: JsonExporter } = require("@opentelemetry/exporter-trace-otlp-http");
const { OTLPTraceExporter: ProtoExporter } = require("@opentelemetry/exporter-trace-otlp-proto");
const { opentelemetry } = require("@opentelemetry/otlp-transformer/build/src/generated/root");
const requestType = opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const sdk = await import(pathToFileURL(require.resolve("@guardrails-ai/core")));
const cm = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(cm);
const received = [];
const server = createServer(async (req, res) => {
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const body = req.headers["content-encoding"] === "gzip" ? gunzipSync(raw) : raw;
    const payload = req.headers["content-type"] === "application/x-protobuf"
      ? requestType.toObject(requestType.decode(body), { longs: String, bytes: String })
      : JSON.parse(body);
    received.push({ headers: req.headers, payload });
    res.writeHead(200, { "content-type": req.headers["content-type"] });
    res.end(req.headers["content-type"] === "application/x-protobuf" ? Buffer.alloc(0) : "{}");
  } catch (error) {
    res.writeHead(500);
    res.end();
    console.error(error);
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const endpoint = `http://127.0.0.1:${server.address().port}/v1/traces`;
const allSpans = () => received.flatMap(r => r.payload.resourceSpans ?? [])
  .flatMap(r => r.scopeSpans ?? []).flatMap(r => r.spans ?? []);
const attr = (s, key) => s.attributes?.find(a => a.key === key)?.value;
const original = sdk.Guard.prototype.parse;
const rail = '<rail version="0.1"><output><integer name="count" /></output></rail>';
const guard = await sdk.Guard.fromRailString(rail);
const privateInput = '{"count":"synthetic-private-invalid-integer"}';
try {
  for (const [protocol, Exporter] of [["json", JsonExporter], ["protobuf", ProtoExporter]]) {
    for (const compression of ["none", "gzip"]) {
      for (const hideContent of [false, true]) {
        received.length = 0;
        const exporter = new Exporter({ url: endpoint, compression, headers: { "x-fi-api-key": "synthetic-test-key" } });
        const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
        const inst = new GuardrailsInstrumentation({ traceConfig: { hideInputs: hideContent, hideOutputs: hideContent, hideErrorDetails: true } });
        inst.setTracerProvider(provider);
        try {
          // Pass the actual immutable ESM namespace, not a fake SDK or facade.
          inst.manuallyInstrument(sdk);
          inst.manuallyInstrument(sdk);
          assert.equal(isPatched(), true);
          const parent = provider.getTracer("application").startSpan("parent");
          const failed = await context.with(trace.setSpan(context.active(), parent), () => guard.parse(privateInput));
          const passed = await context.with(trace.setSpan(context.active(), parent), () => guard.parse('{"count":7}'));
          assert.equal(failed.validationPassed, false);
          assert.equal(passed.validationPassed, true);
          assert.ok(failed instanceof sdk.Outputs.ValidationOutcome);
          assert.equal(passed.validatedOutput.count, 7);
          parent.end();
          await provider.forceFlush();
          const spans = allSpans();
          const guards = spans.filter(s => s.name.startsWith("Guardrails parse"));
          assert.equal(spans.length, 3);
          assert.equal(guards.length, 2);
          assert.deepEqual(guards.map(s => attr(s, "guardrails.validation_passed")?.boolValue ?? false), [false, true]);
          for (const span of guards) {
            assert.equal(attr(span, "fi.span.kind").stringValue, "GUARDRAIL");
            assert.equal(span.status.code, 1); // Validation rejection is an outcome, not a thrown error.
            assert.equal(span.parentSpanId, spans.find(s => s.name === "parent").spanId);
          }
          assert.ok(guards[0].attributes.some(a => a.key === "guardrails.validation_passed"));
          assert.equal(JSON.stringify(spans).includes("synthetic-private-invalid-integer"), !hideContent);
          assert.equal(received[0].headers["x-fi-api-key"], "synthetic-test-key");
          assert.equal(received[0].headers["content-encoding"], compression === "gzip" ? "gzip" : undefined);
          inst.disable();
          assert.equal(sdk.Guard.prototype.parse, original);
          assert.equal(isPatched(), false);
          await guard.parse('{"count":8}');
          await provider.forceFlush();
          assert.equal(allSpans().length, 3);
          inst.enable();
          await guard.parse(privateInput);
          await provider.forceFlush();
          assert.equal(allSpans().filter(s => s.name.startsWith("Guardrails parse")).length, 3);
          console.log(JSON.stringify({ protocol, compression, hideContent, result: "PASS" }));
        } finally {
          inst.disable();
          await provider.shutdown();
        }
      }
    }
  }

  // Execute the README's application verbatim in a fresh process against this
  // receiver, using only the consumer's installed tarball and released core.
  const readme = await readFile(resolve(dirname(fileURLToPath(import.meta.url)), "../README.md"), "utf8");
  const snippet = readme.match(/```javascript\n([\s\S]*?)\n```/)[1];
  const example = await readFile(resolve(dirname(fileURLToPath(import.meta.url)), "../examples/example.mjs"), "utf8");
  assert.equal(example.trim(), snippet.trim());
  const script = resolve(consumer, "readme-example.mjs");
  await writeFile(script, snippet);
  received.length = 0;
  try {
    const exitCode = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [script], {
        env: { ...process.env, FI_BASE_URL: endpoint, FI_API_KEY: "synthetic-readme-key", FI_SECRET_KEY: "synthetic-readme-secret" },
        stdio: "inherit",
      });
      child.on("error", reject);
      child.on("exit", resolve);
    });
    assert.equal(exitCode, 0);
    const spans = allSpans();
    assert.equal(spans.length, 2);
    assert.deepEqual(spans.map(s => attr(s, "guardrails.validation_passed")?.boolValue), [true, false]);
    assert.equal(JSON.stringify(spans).includes("not-an-integer"), false);
    assert.equal(received[0].headers["x-api-key"], "synthetic-readme-key");
    console.log("README: PASS");
  } finally {
    await unlink(script);
  }
} finally {
  sdk.exit();
  cm.disable();
  await new Promise(resolve => server.close(resolve));
}
