/**
 * Contract test against the REAL @cursor/sdk (ESM, Node >= 22.13, network to
 * api.cursor.com). No Cursor account is needed.
 *
 * Wrong key (`apiKey: "<invalid>"`): the SDK fails in `Agent.create()`.
 *   - the caller gets exactly the error it gets without tracing;
 *   - one ERROR span; by default only the error class and structural fields
 *     (no message, no stack trace, no local path); full text only on opt-in;
 *   - `Agent.prompt()` is traced through the patched `Agent.create()`.
 *
 * Missing key (no apiKey, no CURSOR_API_KEY, no stored login): `Agent.create()`
 * succeeds and the REAL local runtime runs `send()` / `stream()` / `wait()`
 * until the backend rejects the run. With the documented setup (provider not
 * registered as the global one, passed explicitly) the exporter receives only
 * this package's spans, none of the SDK's own internal OpenTelemetry spans.
 *
 *   HOME="$(mktemp -d)" pnpm run test:contract
 *
 * Use a throwaway HOME: the SDK keeps a local agent store under ~/.cursor.
 */
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { trace } from "@opentelemetry/api";
import * as CursorSDK from "@cursor/sdk";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CursorSDKInstrumentation, INSTRUMENTATION_NAME, isPatched, shutdown } from "../dist/esm/index.js";

const BAD_KEY = "crsr_th8263_contract_invalid_key_do_not_use";
/** The workspace checkout: the SDK's stack frames point into it (it contains the local username). */
const CHECKOUT = fileURLToPath(new URL("../../..", import.meta.url)).replace(/\/$/, "");
const cwd = mkdtempSync(join(tmpdir(), "cursor-sdk-contract-"));
const options = { apiKey: BAD_KEY, model: { id: "auto" }, local: { cwd } };
delete process.env.CURSOR_API_KEY;

function memory() {
  const exporter = new InMemorySpanExporter();
  return { exporter, provider: new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] }) };
}
// The Future AGI provider of the documented setup: NOT the global provider.
const { exporter, provider } = memory();
// A global provider, standing in for "register() made the FI provider global": catches the SDK's own spans.
const global = memory();
trace.setGlobalTracerProvider(global.provider);

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
const scopeOf = (s) => s.instrumentationScope?.name ?? s.instrumentationLibrary?.name;
/** Everything a span exports: attributes, events and status. */
const exported = (spans) =>
  JSON.stringify(spans.map((s) => ({ name: s.name, attributes: s.attributes, events: s.events.map((e) => ({ name: e.name, attributes: e.attributes })), status: s.status })));

function assertPrivateByDefault(spans, expected) {
  const dump = exported(spans);
  assert.ok(!dump.includes(BAD_KEY), "API key leaked into a span");
  assert.ok(!dump.includes(CHECKOUT), `local checkout path (${CHECKOUT}) leaked into a span`);
  assert.ok(!dump.includes("file://"), "file URL leaked into a span");
  assert.ok(!dump.includes("exception.stacktrace"), "stack trace exported by default");
  assert.ok(!dump.includes("exception.message"), "exception message exported by default");
  if (expected?.message) assert.ok(!dump.includes(expected.message), "raw error message exported by default");
}

function assertCreateErrorSpan(span, expected) {
  assert.equal(span.status.code, 2, "span status must be ERROR");
  assert.equal(span.status.message, `${expected.name} (${expected.status})`);
  assert.equal(span.attributes["cursor.error.message"], `${expected.name} (${expected.status})`);
  assert.equal(span.attributes["fi.span.kind"], "AGENT");
  assert.equal(span.attributes["cursor.error.name"], expected.name);
  if (expected.code) assert.equal(span.attributes["cursor.error.code"], expected.code);
  if (expected.status) assert.equal(span.attributes["cursor.error.http_status"], expected.status);
  assert.equal(span.attributes["cursor.runtime"], "local");
  assert.equal(span.attributes["gen_ai.request.model"], "auto");
  const exception = span.events.find((e) => e.name === "exception");
  assert.ok(exception, "exception event recorded");
  assert.deepEqual(exception.attributes, { "exception.type": expected.name });
}

// 1. Baseline without tracing.
const baselineCreate = describe(await capture(() => CursorSDK.Agent.create(options)));
const baselinePrompt = describe(await capture(() => CursorSDK.Agent.prompt("say hi", options)));
console.log("baseline Agent.create error:", JSON.stringify(baselineCreate));

// 2. Instrument the real module namespace (documented setup: explicit provider).
const instrumentation = new CursorSDKInstrumentation({ tracerProvider: provider });
assert.equal(instrumentation.manuallyInstrument(CursorSDK), true);
assert.equal(isPatched(CursorSDK.Agent), true);

await check("wrong key, Agent.create: same error to the caller, one ERROR span, no message/stack/path by default", async () => {
  const error = describe(await capture(() => CursorSDK.Agent.create(options)));
  assert.deepEqual(error, baselineCreate);
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1, `spans: ${spans.map((s) => s.name)}`);
  assert.equal(spans[0].name, "Cursor Agent create");
  assertCreateErrorSpan(spans[0], error);
  assertPrivateByDefault(spans, error);
  console.log("create span (default):", JSON.stringify(spans[0].status), JSON.stringify(spans[0].events.map((e) => e.attributes)));
});

await check("wrong key, Agent.prompt goes through the patched Agent.create", async () => {
  const error = describe(await capture(() => CursorSDK.Agent.prompt("say hi", options)));
  assert.deepEqual(error, baselinePrompt);
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1, `spans: ${spans.map((s) => s.name)}`);
  assert.equal(spans[0].name, "Cursor Agent create");
  assertCreateErrorSpan(spans[0], error);
  assertPrivateByDefault(spans, error);
});

await check("wrong key with content opted in: full message and the stack trace (which carries the local path)", async () => {
  instrumentation.unpatch();
  const optIn = new CursorSDKInstrumentation({ tracerProvider: provider, traceConfig: { hideInputs: false, hideOutputs: false } });
  optIn.manuallyInstrument(CursorSDK);
  try {
    const error = describe(await capture(() => CursorSDK.Agent.create(options)));
    const spans = exporter.getFinishedSpans();
    assert.equal(spans.length, 1);
    assert.equal(spans[0].status.message, error.message);
    const exception = spans[0].events.find((e) => e.name === "exception");
    assert.equal(exception.attributes["exception.message"], error.message);
    assert.ok(String(exception.attributes["exception.stacktrace"]).includes(CHECKOUT), "opt-in stack trace has the SDK path");
    assert.ok(!exported(spans).includes(BAD_KEY), "API key leaked into a span");
  } finally {
    optIn.unpatch();
    optIn.disable();
    instrumentation.manuallyInstrument(CursorSDK);
  }
});

await check("missing key (no account): the REAL local runtime runs send/stream/wait; run span ERROR; exporter gets 0 SDK-internal spans", async () => {
  global.exporter.reset();
  const noKey = { model: { id: "auto" }, local: { cwd } };
  const agent = await CursorSDK.Agent.create(noKey);
  assert.ok(agent.agentId.startsWith("agent-"), agent.agentId);
  let streamEndStatus;
  const types = [];
  let result;
  try {
    const run = await agent.send("List the files in this folder. CONTRACT-MARKER-7713");
    for await (const event of run.stream()) types.push(event.type === "status" ? `status:${event.status}` : event.type);
    streamEndStatus = run.status;
    result = await run.wait();
  } finally {
    await agent[Symbol.asyncDispose]?.();
  }
  await shutdown(provider);
  console.log("missing-key run: events", types.join(","), "| run.status at stream end:", streamEndStatus, "| wait:", JSON.stringify({ status: result.status, error: result.error }));

  const spans = exporter.getFinishedSpans();
  const foreign = spans.filter((s) => scopeOf(s) !== INSTRUMENTATION_NAME);
  assert.equal(foreign.length, 0, `foreign spans in the FI exporter: ${foreign.map((s) => `${scopeOf(s)}:${s.name}`)}`);
  const run = spans.find((s) => s.name === "Cursor Agent run");
  assert.ok(run, `spans: ${spans.map((s) => s.name)}`);
  assert.equal(run.status.code, 2);
  assert.equal(run.attributes["session.id"], agent.agentId);
  assert.equal(run.attributes["cursor.run.status"], "error");
  assert.ok(/^RunError( \(.+\))?$/.test(run.status.message), `fixed run-error summary, got ${run.status.message}`);
  const dump = exported(spans);
  assert.ok(!dump.includes("CONTRACT-MARKER-7713"), "prompt exported by default");
  assert.ok(!dump.includes(CHECKOUT) && !dump.includes(cwd), "local path exported by default");
  if (result.error?.message) assert.ok(!dump.includes(result.error.message), "Cursor's run-error text exported by default");

  const internal = global.exporter.getFinishedSpans();
  const scopes = [...new Set(internal.map(scopeOf))];
  console.log(`SDK-internal spans on the GLOBAL provider (would reach Future AGI if the FI provider were global): ${internal.length}, scopes ${JSON.stringify(scopes)}, e.g. ${JSON.stringify([...new Set(internal.map((s) => s.name))].slice(0, 6))}`);
  console.log("FI exporter scopes:", JSON.stringify([...new Set(spans.map(scopeOf))]), "spans:", spans.length);
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
const version = JSON.parse(readFileSync(new URL("../node_modules/@cursor/sdk/package.json", import.meta.url), "utf8")).version;
console.log(`${results.length - failed}/${results.length} contract checks passed against @cursor/sdk ${version}`);
process.exit(failed ? 1 : 0);
