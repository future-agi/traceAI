// Real Codex SDK -> real fi-core OTLP/HTTP JSON exporter -> loopback receiver.
// Run under the offline network sandbox, after building the workspace packages.
import assert from "node:assert/strict";
import http from "node:http";
import { fileURLToPath } from "node:url";
import * as InstalledCodexSDK from "@openai/codex-sdk";
import { ProjectType, register } from "@traceai/fi-core";
import { CodexSDKInstrumentation, shutdown } from "../dist/esm/index.js";

const CodexSDK = process.env.CODEX_SDK_ENTRY
  ? await import(process.env.CODEX_SDK_ENTRY)
  : InstalledCodexSDK;
const fake = fileURLToPath(new URL("./fake-codex.mjs", import.meta.url));
const received = [];
const server = http.createServer((request, response) => {
  const chunks = [];
  request.on("data", (chunk) => chunks.push(chunk));
  request.on("end", () => {
    received.push({ path: request.url, method: request.method, headers: request.headers, body: Buffer.concat(chunks) });
    response.writeHead(200);
    response.end();
  });
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
let provider;
try {
  const endpoint = `http://127.0.0.1:${server.address().port}/v1/traces`;
  provider = register({ projectName: "codex-wire-contract", projectType: ProjectType.OBSERVE,
    endpoint, headers: { "X-Api-Key": "synthetic-key", "X-Secret-Key": "synthetic-secret" },
    setGlobalTracerProvider: false });
  const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
  assert.equal(instrumentation.manuallyInstrument(CodexSDK), true);
  const thread = new CodexSDK.Codex({ codexPathOverride: fake,
    env: { PATH: process.env.PATH ?? "", FAKE_CODEX_SCENARIO: "tool" } })
    .startThread({ skipGitRepoCheck: true });
  const result = await thread.run("synthetic prompt WIRE-SECRET-PROMPT");
  assert.equal(result.items.length, 3);
  await shutdown(provider);
  instrumentation.unpatch();
  assert.ok(received.length > 0, "collector must receive OTLP export");
  assert.ok(received.every((message) => message.method === "POST" && message.path === "/v1/traces"));
  assert.ok(received.every((message) => message.headers["content-type"] === "application/json"),
    `unexpected content type: ${received.map((message) => message.headers["content-type"]).join(", ")}`);
  assert.ok(received.every((message) => message.headers["x-api-key"] === "synthetic-key"));
  assert.ok(received.every((message) => message.headers["x-secret-key"] === "synthetic-secret"));
  const spans = received.flatMap((message) => {
    const payload = JSON.parse(message.body.toString("utf8"));
    assert.ok(Array.isArray(payload.resourceSpans), "OTLP/HTTP JSON must contain resourceSpans");
    return payload.resourceSpans.flatMap((resource) => resource.scopeSpans.flatMap((scope) => scope.spans));
  });
  const turn = spans.find((span) => span.name === "codex.turn");
  const command = spans.find((span) => span.name === "codex.command_execution");
  assert.ok(turn && command, "turn and command spans must be exported");
  assert.equal(command.traceId, turn.traceId);
  assert.equal(command.parentSpanId, turn.spanId);
  assert.ok(turn.attributes.some((attribute) => attribute.key === "gen_ai.usage.input_tokens" && String(attribute.value.intValue) === "100"));
  assert.ok(turn.attributes.some((attribute) => attribute.key === "session.id" && attribute.value.stringValue === "thread-contract-1"));
  assert.equal(JSON.stringify(spans).includes("WIRE-SECRET-PROMPT"), false, "private prompt leaked to OTLP");
  console.log(`PASS OTLP wire (${process.env.CODEX_SDK_ENTRY ?? "installed SDK"}): ${received.length} exports`);
} finally {
  if (provider) await provider.shutdown();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
