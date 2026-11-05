// Real SDK + its bundled Codex binary -> loopback Responses API (synthetic only).
// Unlike fake-codex.mjs, this verifies the binary's actual usage semantics.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as CodexSDK from "@openai/codex-sdk";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CodexSDKInstrumentation, isPatched } from "../dist/esm/index.js";

const home = mkdtempSync(join(tmpdir(), "codex-binary-contract-"));
process.env.HOME = home;
process.env.CODEX_HOME = home;
const requests = [];
const wireUsage = [];
const server = http.createServer(async (req, res) => {
  // Recent binaries probe WebSocket support before falling back to HTTP/SSE.
  if (req.method !== "POST" || req.url !== "/v1/responses") {
    res.writeHead(404);
    res.end();
    return;
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString());
  requests.push({ model: body.model, stream: body.stream });
  const n = requests.length;
  const item = { id: `msg_${n}`, type: "message", status: "completed", role: "assistant",
    content: [{ type: "output_text", text: "PRIVATE_BINARY_RESPONSE", annotations: [] }] };
  const response = { id: `resp_${n}`, object: "response", created_at: Math.floor(Date.now() / 1000),
    model: body.model, status: "completed", output: [item],
    usage: { input_tokens: 100, input_tokens_details: { cached_tokens: 40 }, output_tokens: 10,
      output_tokens_details: { reasoning_tokens: 2 }, total_tokens: 110 } };
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  const emit = (type, fields) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
  emit("response.created", { response: { ...response, status: "in_progress", output: [] } });
  emit("response.output_item.added", { output_index: 0, item: { ...item, status: "in_progress", content: [] } });
  emit("response.content_part.added", { item_id: item.id, output_index: 0, content_index: 0,
    part: { type: "output_text", text: "", annotations: [] } });
  emit("response.output_text.delta", { item_id: item.id, output_index: 0, content_index: 0, delta: "PRIVATE_BINARY_RESPONSE" });
  emit("response.output_text.done", { item_id: item.id, output_index: 0, content_index: 0, text: "PRIVATE_BINARY_RESPONSE" });
  emit("response.content_part.done", { item_id: item.id, output_index: 0, content_index: 0, part: item.content[0] });
  emit("response.output_item.done", { output_index: 0, item });
  emit("response.completed", { response });
  res.end();
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));

const exporters = Array.from({ length: 3 }, () => new InMemorySpanExporter());
const providers = exporters.map((exporter) => new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] }));
const instruments = providers.map((tracerProvider) => new CodexSDKInstrumentation({ tracerProvider }));
const turns = (exporter) => exporter.getFinishedSpans().filter((span) => span.name === "codex.turn");
const options = { model: "gpt-5.1-codex", skipGitRepoCheck: true, sandboxMode: "danger-full-access", approvalPolicy: "never" };
const client = () => new CodexSDK.Codex({ baseUrl: `http://127.0.0.1:${server.address().port}/v1`,
  apiKey: "synthetic-codex-key", env: { PATH: process.env.PATH, HOME: home, CODEX_HOME: home, RUST_LOG: "error" } });

try {
  const thread = client().startThread(options);
  instruments[0].wrapThread(thread);
  const first = await thread.run("PRIVATE_BINARY_PROMPT_1", { signal: AbortSignal.timeout(45000) });
  assert.equal(first.finalResponse, "PRIVATE_BINARY_RESPONSE");
  wireUsage.push(first.usage);
  instruments[0].unpatch();
  assert.equal(isPatched(thread), false);

  instruments[1].wrapThread(thread);
  // A second unpatch of the old owner must not remove the new owner's wrapper.
  instruments[0].unpatch();
  const { events } = await thread.runStreamed("PRIVATE_BINARY_PROMPT_2", { signal: AbortSignal.timeout(45000) });
  for await (const event of events) {
    if (event.type === "turn.completed") wireUsage.push(event.usage);
  }
  instruments[1].unpatch();

  // The old instance patch must not shadow a later prototype patch.
  assert.equal(instruments[2].manuallyInstrument(CodexSDK), true);
  const third = await thread.run("PRIVATE_BINARY_PROMPT_3", { signal: AbortSignal.timeout(45000) });
  wireUsage.push(third.usage);
  const resumed = client().resumeThread(thread.id, options);
  const fourth = await resumed.run("PRIVATE_BINARY_PROMPT_4", { signal: AbortSignal.timeout(45000) });
  wireUsage.push(fourth.usage);

  assert.equal(requests.length, 4, "exactly one provider request per turn");
  assert.ok(requests.every((request) => request.model === options.model && request.stream === true));
  assert.deepEqual(exporters.map((exporter) => turns(exporter).length), [1, 1, 2], "each owner exports only its turns");
  assert.deepEqual(wireUsage.map((usage) => [usage.input_tokens, usage.output_tokens, usage.cached_input_tokens]),
    [[100, 10, 40], [200, 20, 80], [300, 30, 120], [400, 40, 160]], "supported binaries report cumulative usage");

  const spans = exporters.flatMap((exporter) => exporter.getFinishedSpans());
  const turnSpans = spans.filter((span) => span.name === "codex.turn");
  for (const [index, turn] of turnSpans.entries()) {
    assert.equal(turn.attributes["gen_ai.usage.input_tokens"], 100);
    assert.equal(turn.attributes["gen_ai.usage.output_tokens"], 10);
    assert.equal(turn.attributes["gen_ai.usage.cache_read_tokens"], 40);
    assert.equal(turn.attributes["gen_ai.usage.total_tokens"], 110);
    assert.equal(turn.attributes["codex.cumulative.input_tokens"], 100 * (index + 1));
    assert.equal(turn.attributes["codex.usage.baseline_unknown"], undefined);
    assert.equal(turn.attributes["gen_ai.request.model"], options.model);
    assert.equal(turn.attributes["gen_ai.provider.name"], "openai");
    assert.equal(turn.attributes["fi.span.kind"], "AGENT");
    assert.equal(turn.attributes["session.id"], thread.id);
    assert.equal(turn.status.code, 1);
    assert.ok(spans.some((span) => span.name === "codex.agent_message" && span.parentSpanContext?.spanId === turn.spanContext().spanId));
  }
  const encoded = JSON.stringify(spans.map((span) => ({ name: span.name, attributes: span.attributes, status: span.status, events: span.events })));
  for (const hidden of ["synthetic-codex-key", "PRIVATE_BINARY_PROMPT", "PRIVATE_BINARY_RESPONSE", home]) {
    assert.equal(encoded.includes(hidden), false, `${hidden} must not be exported`);
  }
  const sdk = JSON.parse(readFileSync(new URL("../node_modules/@openai/codex-sdk/package.json", import.meta.url))).version;
  console.log(JSON.stringify({ status: "PASS", sdk, requests: requests.length, wireUsage,
    exportedUsage: turnSpans.map((span) => ({ input: span.attributes["gen_ai.usage.input_tokens"],
      output: span.attributes["gen_ai.usage.output_tokens"], cached: span.attributes["gen_ai.usage.cache_read_tokens"] })),
    lifecycle: "instance -> fresh instance/provider -> class -> resumeThread" }, null, 2));
} finally {
  for (const instrumentation of instruments) instrumentation.unpatch();
  await Promise.all(providers.map((provider) => provider.shutdown()));
  await new Promise((resolve) => server.close(resolve));
  rmSync(home, { recursive: true, force: true });
}
