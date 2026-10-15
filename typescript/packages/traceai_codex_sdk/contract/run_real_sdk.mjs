/**
 * Contract test against the REAL @openai/codex-sdk (ESM, so it runs outside
 * jest): the SDK spawns `fake-codex.mjs` through `codexPathOverride` and the
 * built package traces it into an in-memory exporter. No network, no model.
 *
 *   pnpm run test:contract
 */
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import * as CodexSDK from "@openai/codex-sdk";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CodexSDKInstrumentation, isPatched } from "../dist/esm/index.js";

const FAKE = fileURLToPath(new URL("./fake-codex.mjs", import.meta.url));
const MARKER = "MARKER-CONTRACT-3141";
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });

function codex(scenario) {
  return new CodexSDK.Codex({ codexPathOverride: FAKE, env: { PATH: process.env.PATH ?? "", FAKE_CODEX_SCENARIO: scenario } });
}
const turns = () => exporter.getFinishedSpans().filter((s) => s.name === "codex.turn");
const exported = () =>
  JSON.stringify(
    exporter.getFinishedSpans().map((s) => ({ attributes: s.attributes, status: s.status, events: s.events.map((e) => ({ name: e.name, attributes: e.attributes })) })),
  );
function assertNoLeak(marker) {
  const text = exported();
  for (const leak of [marker, "/Users/", "node_modules", "AWS_SECRET", "config.toml"]) {
    assert.equal(text.includes(leak), false, `exported spans contain ${leak}`);
  }
}
const childrenOf = (turn) =>
  exporter.getFinishedSpans().filter((s) => (s.parentSpanContext?.spanId ?? s.parentSpanId) === turn.spanContext().spanId);
const results = [];
async function check(name, fn) {
  exporter.reset();
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.stack ?? error}`);
  }
}

const hidden = new CodexSDKInstrumentation({ tracerProvider: provider });
assert.equal(hidden.manuallyInstrument(CodexSDK), true);
assert.equal(isPatched(CodexSDK.Thread.prototype), true);

await check("run(): one turn, tool child, session.id, tokens, content hidden", async () => {
  const thread = codex("tool").startThread({ model: "gpt-test", skipGitRepoCheck: true });
  const result = await thread.run(`list files ${MARKER}`);
  assert.equal(result.items.length, 3);
  assert.equal(result.finalResponse.includes(MARKER), true, "SDK result is unchanged");
  const [turn] = turns();
  assert.equal(turns().length, 1);
  assert.equal(turn.attributes["session.id"], "thread-contract-1");
  assert.equal(turn.attributes["fi.span.kind"], "AGENT");
  assert.equal(turn.attributes["gen_ai.request.model"], "gpt-test");
  assert.equal(turn.attributes["gen_ai.usage.input_tokens"], 100);
  assert.equal(turn.attributes["gen_ai.usage.output_tokens"], 10);
  assert.deepEqual(childrenOf(turn).map((s) => s.name), ["codex.agent_message", "codex.command_execution", "codex.agent_message"]);
  const tool = childrenOf(turn).find((s) => s.name === "codex.command_execution");
  assert.equal(tool.attributes["fi.span.kind"], "TOOL");
  assert.equal(tool.attributes["codex.command.exit_code"], 0);
  assert.ok(tool.endTime[0] * 1e9 + tool.endTime[1] > tool.startTime[0] * 1e9 + tool.startTime[1], "tool span has duration");
  assert.equal(JSON.stringify(exporter.getFinishedSpans().map((s) => s.attributes)).includes(MARKER), false);

  // Second turn on the same thread: the binary is re-run with `resume <id>` and reports a running total.
  await thread.run("again");
  const second = turns().at(-1);
  assert.equal(second.attributes["codex.thread.resumed"], true);
  assert.equal(second.attributes["gen_ai.usage.input_tokens"], 100);
  assert.equal(second.attributes["codex.cumulative.input_tokens"], 200);
});

await check("runStreamed(): break before completion is cancelled", async () => {
  const thread = codex("tool").startThread({ skipGitRepoCheck: true });
  const { events } = await thread.runStreamed("go");
  for await (const event of events) {
    if (event.type === "item.started") break;
  }
  const [turn] = turns();
  assert.equal(turn.status.code, 2);
  assert.equal(turn.attributes["codex.cancelled"], true);
});

await check("turn.failed: run() rejects, turn ERROR with a fixed label", async () => {
  const thread = codex("failed").startThread({ skipGitRepoCheck: true });
  await assert.rejects(thread.run("go"), /model provider returned 500/);
  const [turn] = turns();
  assert.equal(turn.status.code, 2);
  assert.equal(turn.status.message, "turn failed");
});

await check("binary exits non-zero with a path and the prompt on stderr: summary only, no leak", async () => {
  const thread = codex("crash").startThread({ skipGitRepoCheck: true });
  await assert.rejects(thread.run(`go ${MARKER}`), (error) => error.message.includes("/Users/probeuser") && error.message.includes(MARKER));
  const [turn] = turns();
  assert.equal(turn.status.code, 2);
  assert.equal(turn.status.message, "Codex Exec exited with code 1");
  const exception = turn.events.find((e) => e.name === "exception");
  assert.deepEqual(exception.attributes, { "exception.type": "Error", "exception.message": "Codex Exec exited with code 1" });
  assertNoLeak(MARKER);
});

await check("unparseable stdout line: summary only, no leak", async () => {
  const thread = codex("badline").startThread({ skipGitRepoCheck: true });
  await assert.rejects(thread.run(`go ${MARKER}`), /Failed to parse item/);
  const [turn] = turns();
  assert.equal(turn.status.code, 2);
  assert.equal(turn.status.message, "Failed to parse item");
  assertNoLeak(MARKER);
});

await check("retried stream error then turn.completed: turn OK, retry counted, text hidden", async () => {
  const thread = codex("retry").startThread({ skipGitRepoCheck: true });
  const result = await thread.run(`go ${MARKER}`);
  assert.equal(result.items.length, 3);
  const [turn] = turns();
  assert.equal(turn.status.code, 1, "OK");
  assert.equal(turn.attributes["codex.stream_error.count"], 1);
  assert.equal(childrenOf(turn).every((s) => s.status.code === 1), true);
  assertNoLeak(MARKER);
});

hidden.unpatch();
assert.equal(isPatched(CodexSDK.Thread.prototype), false);

await check("unpatched: no spans", async () => {
  await codex("tool").startThread({ skipGitRepoCheck: true }).run("go");
  assert.equal(turns().length, 0);
});

await check("wrapCodex() with content opt-in records the prompt", async () => {
  const visible = new CodexSDKInstrumentation({ tracerProvider: provider, traceConfig: { hideInputs: false, hideOutputs: false } });
  const client = visible.wrapCodex(codex("tool"));
  await client.startThread({ skipGitRepoCheck: true }).run(`hello ${MARKER}`);
  const [turn] = turns();
  assert.equal(turn.attributes["input.value"], `hello ${MARKER}`);
  assert.equal(String(turn.attributes["output.value"]).includes(MARKER), true);
  visible.unpatch();
});

console.log(results.join("\n"));
const failed = results.filter((r) => r.startsWith("FAIL")).length;
console.log(`\n${results.length - failed} passed, ${failed} failed (@openai/codex-sdk ${CodexSDK.Codex ? "loaded" : "missing"})`);
process.exit(failed ? 1 : 0);
