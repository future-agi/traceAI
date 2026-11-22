// Copy into an isolated consumer with the actual SDK, fi-core and conventions
// tarballs. Deliberately no reader/carrier/registry injections or network calls.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { trace } from "@opentelemetry/api";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
const require = createRequire(import.meta.url);
const [core, sdk] = await Promise.all([
  process.argv[2] === "cjs" ? require("@traceai/fi-core") : import("@traceai/fi-core"),
  process.argv[3] === "cjs" ? require("@future-agi/sdk/telemetry") : import("@future-agi/sdk/telemetry"),
]);
for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
process.env.FI_SKILLS_TRACING = "off";
sdk.configure({skillsTracing: "on"});
const exporter = new InMemorySpanExporter();
// Runtime attribution is opt-in per provider; the SDK modes narrow it from there.
const provider = core.register({projectName: "local-mode-check", runtimeAttributes: true, spanProcessors: [new SimpleSpanProcessor(exporter)]});
const tracer = trace.getTracer("mode-check");
const versions = [1, 2, 3].map(n => `22222222-2222-4222-8222-22222222222${n}`);
const declare = i => sdk.declareSkill({kind: "skill", resourceId: "11111111-1111-4111-8111-111111111111", versionId: versions[i],
  name: "sample", version: i + 1, digest: "sha256:" + "a".repeat(64), requested: {version: i + 1}});
try {
  let traceId;
  await tracer.startActiveSpan("root", async root => {
    try {
      traceId = root.spanContext().traceId;
      declare(0); // Seed the real automatic registry; no explicit scope/carrier.
      tracer.startSpan("explicit-on").end();
      process.env.FI_SKILLS_TRACING = "on";
      sdk.configure({skillsTracing: "off"});
      declare(1);
      tracer.startSpan("explicit-off").end();
      sdk.configure({skillsTracing: "load_only"});
      declare(2);
      tracer.startSpan("load-only").end();
      process.env.FI_SKILLS_TRACING = "off";
      sdk.configure({skillsTracing: "on"});
      tracer.startSpan("resumed").end();
      sdk.withAgentScope({agent: "sample-agent"}, () => {
        tracer.startSpan("scope-on").end();
        sdk.configure({skillsTracing: "off"});
        tracer.startSpan("scope-off").end();
      });
      sdk.configure(); // No explicit setting: env off, then on, on the same trace.
      tracer.startSpan("reset-off").end();
      process.env.FI_SKILLS_TRACING = "on";
      tracer.startSpan("reset-on").end();
    } finally { root.end(); }
  });
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans();
  assert.ok(spans.every(s => s.spanContext().traceId === traceId));
  const attrs = name => { const span = spans.find(s => s.name === name); assert.ok(span, name); return span.attributes; };
  for (const name of ["explicit-on", "resumed", "reset-on"]) assert.deepEqual(attrs(name)["fi.skills.version_ids"], [versions[0]], name);
  for (const name of ["explicit-off", "load-only", "scope-off", "reset-off"]) {
    assert.equal(attrs(name)["fi.skills.version_ids"], undefined, name);
    assert.equal(attrs(name)["gen_ai.agent.name"], undefined, name);
  }
  assert.equal(attrs("scope-on")["gen_ai.agent.name"], "sample-agent");
  assert.deepEqual(spans.filter(s => s.name === "skill.load").map(s => s.attributes["fi.skill.version_id"]), [versions[0], versions[2]]);
  console.log(`PASS: one trace; explicit on/off/load_only/reset, real SDK registry and scope (${process.argv.slice(2).join("/")})`);
} finally { sdk.configure(); await provider.shutdown(); }
