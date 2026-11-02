// Run from an isolated consumer that installed the built @traceai/langchain
// tarball and @langchain/langgraph-sdk at either the first or latest release.
// All SDK traffic and OTLP exports go to ephemeral loopback HTTP servers.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { Client } from "@langchain/langgraph-sdk";
import { wrapLangGraphClient, langGraphTraceRequestHook } from "@traceai/langchain";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { context, propagation } from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";

const version = process.argv[2];
assert.ok(["0.0.1", "1.12.9"].includes(version));
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
propagation.setGlobalPropagator(new W3CTraceContextPropagator());
const received = [];
const collector = createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  received.push({ path: req.url, body: JSON.parse(Buffer.concat(chunks).toString()) });
  res.writeHead(200, { "content-type": "application/json" });
  res.end("{}");
});
collector.listen(0, "127.0.0.1");
await once(collector, "listening");
const requests = [];
const server = createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  requests.push({ path: req.url, headers: req.headers, body: JSON.parse(Buffer.concat(chunks).toString()) });
  if (req.url.endsWith("/wait")) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ answer: "synthetic-secret-output" }));
  } else if (req.url.endsWith("/stream")) {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end('event: values\ndata: {"answer":"synthetic-secret-output"}\n\nevent: end\ndata: {}\n\n');
  } else {
    res.writeHead(404); res.end();
  }
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({
  url: `http://127.0.0.1:${collector.address().port}/v1/traces`,
}))] });
try {
  const config = { apiUrl: `http://127.0.0.1:${server.address().port}` };
  if (version !== "0.0.1") {
    Object.assign(config, { apiKey: null, onRequest: langGraphTraceRequestHook() });
  }
  // Negative control: the unwrapped remote SDK makes a real request but emits
  // no useful client span, even when its native request hook is configured.
  await new Client(config).runs.wait("synthetic-thread", "synthetic-assistant", { input: "synthetic-secret-input" });
  await provider.forceFlush();
  assert.equal(received.length, 0, "unwrapped SDK unexpectedly exported a span");
  requests.length = 0;
  const client = wrapLangGraphClient(new Client(config), { tracerProvider: provider });
  assert.equal(wrapLangGraphClient(client), client);
  assert.deepEqual(await client.runs.wait("synthetic-thread", "synthetic-assistant", { input: "synthetic-secret-input" }),
    { answer: "synthetic-secret-output" });
  const stream = client.runs.stream("synthetic-thread", "synthetic-assistant", { input: "synthetic-secret-input" });
  const events = [];
  for await (const event of stream) events.push(event);
  assert.ok(events.some((event) => event.event === "values"), `stream events: ${JSON.stringify(events)}`);
  assert.equal(requests.length, 2);
  assert.ok(requests.every((item) => item.body.assistant_id === "synthetic-assistant"));
  if (version !== "0.0.1") {
    assert.ok(requests.every((item) => /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/.test(item.headers.traceparent || "")),
      "official request hook must carry the client span context");
  }
  await provider.forceFlush();
  assert.ok(received.length > 0, "OTLP collector received no export");
  const spans = received.flatMap((r) => r.body.resourceSpans.flatMap((resource) =>
    resource.scopeSpans.flatMap((scope) => scope.spans)));
  assert.equal(spans.length, 2, "wait and stream each yield one client span");
  assert.deepEqual(spans.map((s) => s.name).sort(), ["LangGraph runs.stream", "LangGraph runs.wait"]);
  const encoded = JSON.stringify(received);
  assert.ok(!encoded.includes("synthetic-secret-input") && !encoded.includes("synthetic-secret-output"),
    "private request/response data must not enter OTLP");
  assert.ok(spans.every((span) => span.attributes.some((attr) => attr.key === "session.id" && attr.value.stringValue === "synthetic-thread")));
  assert.ok(received.every((r) => r.path === "/v1/traces"));
  console.log(`PASS remote SDK ${version}: wait, stream, OTLP and request hook`);
} finally {
  await provider.shutdown();
  await new Promise((resolve) => server.close(resolve));
  await new Promise((resolve) => collector.close(resolve));
  context.disable();
  propagation.disable();
}
