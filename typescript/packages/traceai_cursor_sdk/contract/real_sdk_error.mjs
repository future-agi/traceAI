/**
 * Contract test against the REAL @cursor/sdk (ESM, Node >= 22.13, network to
 * api.cursor.com). No Cursor account is needed: it uses a deliberately
 * invalid API key, so the SDK fails in `Agent.create()` the way a customer
 * with a bad or missing key sees it, and checks that:
 *
 *   - the caller gets exactly the error it gets without tracing
 *     (same class, message, code, status);
 *   - the failure is one ERROR span with the exception recorded;
 *   - `Agent.prompt()` is traced through the patched `Agent.create()`;
 *   - the API key never appears on a span.
 *
 *   HOME="$(mktemp -d)" pnpm run test:contract
 *
 * Use a throwaway HOME: the SDK keeps a local agent store under ~/.cursor.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as CursorSDK from "@cursor/sdk";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CursorSDKInstrumentation, isPatched } from "../dist/esm/index.js";

const BAD_KEY = "crsr_th8263_contract_invalid_key_do_not_use";
const cwd = mkdtempSync(join(tmpdir(), "cursor-sdk-contract-"));
const options = { apiKey: BAD_KEY, model: { id: "auto" }, local: { cwd } };
delete process.env.CURSOR_API_KEY;

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const results = [];

async function capture(fn) {
  try {
    await fn();
  } catch (error) {
    return error;
  }
  throw new Error("expected the SDK call to throw");
}

const describe = (error) => ({
  name: error?.name,
  message: error?.message,
  code: error?.code,
  status: error?.status,
  isAuthenticationError: error instanceof CursorSDK.AuthenticationError,
  isCursorSdkError: error instanceof CursorSDK.CursorSdkError,
});

async function check(name, fn) {
  exporter.reset();
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error?.stack ?? error}`);
  }
}

// 1. Baseline without tracing.
const baselineCreate = describe(await capture(() => CursorSDK.Agent.create(options)));
const baselinePrompt = describe(await capture(() => CursorSDK.Agent.prompt("say hi", options)));
console.log("baseline Agent.create error:", JSON.stringify(baselineCreate));

// 2. Instrument the real module namespace.
const instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider });
assert.equal(instrumentation.manuallyInstrument(CursorSDK), true);
assert.equal(isPatched(CursorSDK.Agent), true);

function assertNoKey() {
  const dump = JSON.stringify(
    exporter.getFinishedSpans().map((s) => ({ name: s.name, attributes: s.attributes, events: s.events })),
  );
  assert.ok(!dump.includes(BAD_KEY), "API key leaked into a span");
}

function assertErrorSpan(span, expected) {
  assert.equal(span.status.code, 2, "span status must be ERROR");
  assert.equal(span.status.message, expected.message);
  assert.equal(span.attributes["fi.span.kind"], "AGENT");
  assert.equal(span.attributes["cursor.error.name"], expected.name);
  if (expected.code) assert.equal(span.attributes["cursor.error.code"], expected.code);
  if (expected.status) assert.equal(span.attributes["cursor.error.http_status"], expected.status);
  assert.equal(span.attributes["cursor.runtime"], "local");
  assert.equal(span.attributes["gen_ai.request.model"] ?? span.attributes["llm.model_name"], "auto");
  const exception = span.events.find((e) => e.name === "exception");
  assert.ok(exception, "exception event recorded");
  assert.equal(exception.attributes["exception.message"], expected.message);
}

await check("Agent.create with an invalid key: same error to the caller, one ERROR span", async () => {
  const error = describe(await capture(() => CursorSDK.Agent.create(options)));
  assert.deepEqual(error, baselineCreate);
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1, `spans: ${spans.map((s) => s.name)}`);
  assert.equal(spans[0].name, "Cursor Agent create");
  assertErrorSpan(spans[0], error);
  assertNoKey();
  console.log("create span:", spans[0].name, JSON.stringify(spans[0].status), JSON.stringify(spans[0].attributes));
});

await check("Agent.prompt with an invalid key goes through the patched Agent.create", async () => {
  const error = describe(await capture(() => CursorSDK.Agent.prompt("say hi", options)));
  assert.deepEqual(error, baselinePrompt);
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1, `spans: ${spans.map((s) => s.name)}`);
  assert.equal(spans[0].name, "Cursor Agent create");
  assertErrorSpan(spans[0], error);
  assertNoKey();
});

await check("Agent.resume of a missing local agent: same error to the caller, one ERROR span", async () => {
  instrumentation.disable();
  const baseline = describe(await capture(() => CursorSDK.Agent.resume("agent-00000000-0000-4000-8000-000000000000", options)));
  instrumentation.enable();
  exporter.reset();
  const error = describe(await capture(() => CursorSDK.Agent.resume("agent-00000000-0000-4000-8000-000000000000", options)));
  assert.deepEqual(error, baseline);
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1, `spans: ${spans.map((s) => s.name)}`);
  assert.equal(spans[0].name, "Cursor Agent resume");
  assert.equal(spans[0].status.code, 2);
  assert.equal(spans[0].attributes["cursor.agent.id"], "agent-00000000-0000-4000-8000-000000000000");
  assertNoKey();
  console.log("resume error:", JSON.stringify(error));
});

await check("unpatch restores the SDK's own Agent.create", async () => {
  instrumentation.unpatch();
  assert.equal(isPatched(CursorSDK.Agent), false);
  await capture(() => CursorSDK.Agent.create(options));
  assert.equal(exporter.getFinishedSpans().length, 0);
});

rmSync(cwd, { recursive: true, force: true });
console.log(results.join("\n"));
const failed = results.filter((r) => r.startsWith("FAIL")).length;
console.log(`${results.length - failed}/${results.length} contract checks passed against @cursor/sdk ${await sdkVersion()}`);
process.exit(failed ? 1 : 0);

async function sdkVersion() {
  const { readFileSync } = await import("node:fs");
  const pkg = new URL("../node_modules/@cursor/sdk/package.json", import.meta.url);
  return JSON.parse(readFileSync(pkg, "utf8")).version;
}
