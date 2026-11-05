"use strict";

// Real SDK, stalled loopback SSE transport, synthetic credentials only.
// Usage: node release-lifecycle.cjs <SDK alias> <normal|privacy> <fixture directory>
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { createRequire } = require("node:module");
const fixtureRequire = createRequire(path.resolve(process.argv[4] || "/tmp/fixtures", "package.json"));
const sdk = process.argv[2];
const privacy = process.argv[3] === "privacy";
if (privacy) {
  process.env.FI_HIDE_INPUTS = "true";
  process.env.FI_HIDE_OUTPUTS = "true";
  process.env.FI_HIDE_ERROR_DETAILS = "true";
}
const { Anthropic } = fixtureRequire(sdk);
const { trace, context, SpanKind, SpanStatusCode } = fixtureRequire("@opentelemetry/api");
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = fixtureRequire("@opentelemetry/sdk-trace-base");
const { AsyncLocalStorageContextManager } = fixtureRequire("@opentelemetry/context-async-hooks");
const { AnthropicInstrumentation } = require("../dist/src/instrumentation.js");
const native = "openTelemetry" in Anthropic.prototype;
const params = { model: "synthetic-model", max_tokens: 16, messages: [{ role: "user", content: "INPUT_CANARY" }] };
const message = {
  id: "msg_mock", type: "message", role: "assistant", model: "synthetic-model",
  content: [{ type: "text", text: "OUTPUT_CANARY" }], stop_reason: "end_turn", stop_sequence: null,
  usage: { input_tokens: 3, output_tokens: 2, cache_creation_input_tokens: 4, cache_read_input_tokens: 5 },
};
const events = [
  { type: "message_start", message: { ...message, content: [], stop_reason: null, usage: { ...message.usage, output_tokens: 0 } } },
  { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
  { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "OUTPUT_CANARY" } },
  { type: "content_block_stop", index: 0 },
  { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 2 } },
  { type: "message_stop" },
];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let activeConnections = 0;
const server = http.createServer(async (request, response) => {
  let input = "";
  for await (const chunk of request) input += chunk;
  const body = JSON.parse(input);
  response.setHeader("request-id", "synthetic-request");
  if (body.model === "error") {
    response.writeHead(400, { "content-type": "application/json" });
    response.end(JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "ERROR_CANARY" } }));
    return;
  }
  if (body.stream) {
    response.writeHead(200, { "content-type": "text/event-stream" });
    const selected = ["stall", "truncated"].includes(body.model) ? events.slice(0, 3) : events;
    for (const event of selected) response.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    if (body.model === "stall") {
      activeConnections++;
      response.on("close", () => activeConnections--);
    } else {
      response.end();
    }
  } else {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify(message));
  }
});

async function main() {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  const contextManager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(contextManager);
  trace.setGlobalTracerProvider(provider);
  const options = {
    apiKey: "SYNTHETIC_API_KEY", baseURL: `http://127.0.0.1:${server.address().port}`, maxRetries: 0,
    ...(native ? { openTelemetry: { tracerProvider: provider } } : {}),
  };
  const rows = [];
  const failed = [];
  const instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });

  async function check(name, run) {
    try {
      await run();
      rows.push({ name, pass: true });
    } catch (error) {
      rows.push({ name, pass: false, error: String(error) });
      failed.push(name);
    }
  }

  async function cancellation(label) {
    memory.reset();
    const client = new Anthropic(options);
    const stream = await client.messages.create({ ...params, model: "stall", stream: true });
    try {
      for await (const event of stream) {
        assert.equal(event.type, "message_start");
        break;
      }
      for (let i = 0; i < 100 && activeConnections; i++) await delay(10);
      const result = {
        label, aborted: stream.controller.signal.aborted,
        connections: activeConnections, spans: memory.getFinishedSpans().length,
        status: memory.getFinishedSpans()[0]?.status.code,
      };
      rows.push(result);
      return result;
    } finally {
      // Cleanup is unconditional, including when reproducing the old stalled tee.
      stream.controller.abort();
      for (let i = 0; i < 100 && activeConnections; i++) await delay(10);
    }
  }

  try {
    const plain = await cancellation("plain-cancellation");
    assert.equal(plain.aborted, true);
    assert.equal(plain.connections, 0);
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
    instrumentation.manuallyInstrument(Anthropic);
    const client = new Anthropic(options);

    await check("parent, kind, model, provider, IO, and stream", async () => {
      memory.reset();
      await provider.getTracer("qualification").startActiveSpan("parent", async root => {
        try {
          assert.equal((await client.messages.create(params)).id, "msg_mock");
          const stream = await client.messages.create({ ...params, stream: true });
          let last;
          for await (const event of stream) last = event;
          assert.equal(last.type, "message_stop");
          const calls = memory.getFinishedSpans();
          assert.equal(calls.length, 2);
          for (const span of calls) {
            assert.equal(span.parentSpanContext.spanId, root.spanContext().spanId);
            assert.equal(span.kind, native ? SpanKind.CLIENT : SpanKind.INTERNAL);
            assert.equal(span.attributes["gen_ai.provider.name"], "anthropic");
            assert.equal(span.attributes["gen_ai.request.model"], "synthetic-model");
            assert.equal(span.attributes["gen_ai.usage.output_tokens"], 2);
            assert.equal(span.status.code, SpanStatusCode.OK);
            const attributes = JSON.stringify(span.attributes);
            assert(!attributes.includes("SYNTHETIC_API_KEY"));
            if (native || privacy) {
              assert(!attributes.includes("INPUT_CANARY"));
              assert(!attributes.includes("OUTPUT_CANARY"));
            } else {
              assert(attributes.includes("INPUT_CANARY"));
              assert(attributes.includes("OUTPUT_CANARY"));
            }
          }
        } finally {
          root.end();
        }
      });
    });

    for (const streaming of [false, true]) {
      await check(`cache inclusive usage stream=${streaming}`, async () => {
        memory.reset();
        const result = await client.messages.create({ ...params, stream: streaming });
        if (streaming) for await (const _event of result) { /* consume */ }
        const spans = memory.getFinishedSpans();
        assert.equal(spans.length, 1);
        const attributes = spans[0].attributes;
        rows.push({ label: "cache", streaming, attributes });
        assert.equal(attributes["gen_ai.usage.input_tokens"], 12);
        assert.equal(attributes[native ? "gen_ai.usage.cache_write.input_tokens" : "gen_ai.usage.cache_write_tokens"], 4);
        assert.equal(attributes[native ? "gen_ai.usage.cache_read.input_tokens" : "gen_ai.usage.cache_read_tokens"], 5);
        if (!native) assert.equal(attributes["gen_ai.usage.total_tokens"], 14);
      });
    }
    if (!native) {
      const collect = async stream => {
        const received = [];
        for await (const event of stream) received.push(event.type);
        return received;
      };
      const assertCompletion = () => {
        const spans = memory.getFinishedSpans();
        assert.equal(spans.length, 1, "one consumed request must export exactly one span");
        assert.equal(spans[0].status.code, SpanStatusCode.OK);
        assert.equal(spans[0].attributes["gen_ai.usage.input_tokens"], 12);
        assert.equal(spans[0].attributes["gen_ai.usage.output_tokens"], 2);
        assert.equal(spans[0].attributes["gen_ai.usage.total_tokens"], 14);
        assert.equal(spans[0].events.length, 0);
        assert.equal(JSON.stringify(spans[0].attributes).includes("OUTPUT_CANARY"), !privacy);
      };
      const expectedEvents = events.map(event => event.type);
      for (const bothBranches of [false, true]) {
        await check(`tee preserves one traced completion (both=${bothBranches})`, async () => {
          memory.reset();
          const stream = await client.messages.create({ ...params, stream: true });
          try {
            const [left, right] = stream.tee();
            assert.equal(memory.getFinishedSpans().length, 0);
            if (bothBranches) {
              assert.deepEqual(await Promise.all([collect(left), collect(right)]), [expectedEvents, expectedEvents]);
            } else {
              assert.deepEqual(await collect(left), expectedEvents);
              assertCompletion();
              // The slower branch replays buffered events, not a second request span.
              assert.deepEqual(await collect(right), expectedEvents);
            }
            assertCompletion();
            stream.controller.abort();
            assertCompletion();
          } finally {
            stream.controller.abort();
          }
        });
      }
      for (const unusedTee of [false, true]) {
        await check(`ownership begins at first next, not iterator creation (unusedTee=${unusedTee})`, async () => {
          memory.reset();
          const stream = await client.messages.create({ ...params, stream: true });
          try {
            const unused = unusedTee ? stream.tee() : stream[Symbol.asyncIterator]();
            // Merely requesting either kind of iterator must not own or drain the stream.
            await delay(20);
            assert.equal(memory.getFinishedSpans().length, 0);
            assert.deepEqual(await collect(stream), expectedEvents);
            assertCompletion();
            if (!unusedTee) await unused.return();
            assertCompletion();
          } finally {
            stream.controller.abort();
          }
        });
      }
      for (const closeMethod of ["return", "throw"]) {
        await check(`${closeMethod} before first next cannot finish or later claim the stream`, async () => {
          memory.reset();
          const stream = await client.messages.create({ ...params, stream: true });
          try {
            const unused = stream[Symbol.asyncIterator]();
            if (closeMethod === "return") {
              assert.deepEqual(await unused.return("unused"), { done: true, value: "unused" });
            } else {
              const error = new Error("unstarted iterator closed");
              await assert.rejects(unused.throw(error), candidate => candidate === error);
            }
            const early = memory.getFinishedSpans().length;
            assert.equal((await unused.next()).done, true);
            assert.equal(stream.controller.signal.aborted, false);
            assert.deepEqual(await collect(stream), expectedEvents);
            assert.equal(early, 0, "closing an unstarted iterator is not request cancellation");
            assertCompletion();
          } finally {
            stream.controller.abort();
          }
        });
      }
      await check("unused iterator cannot prevent a tee consumer from owning the span", async () => {
        memory.reset();
        const stream = await client.messages.create({ ...params, stream: true });
        try {
          const unused = stream[Symbol.asyncIterator]();
          const [left, right] = stream.tee();
          assert.deepEqual(await Promise.all([collect(left), collect(right)]), [expectedEvents, expectedEvents]);
          assertCompletion();
          await unused.return();
          assertCompletion();
        } finally {
          stream.controller.abort();
        }
      });
      await check("tee observes only requested events and controller cancellation", async () => {
        memory.reset();
        const stream = await client.messages.create({ ...params, stream: true });
        try {
          const [left, right] = stream.tee();
          const first = left[Symbol.asyncIterator]();
          const second = right[Symbol.asyncIterator]();
          assert.equal((await first.next()).value.type, "message_start");
          assert.equal((await second.next()).value.type, "message_start");
          await delay(20);
          assert.equal(memory.getFinishedSpans().length, 0, "instrumentation must not drain the tee");
          stream.controller.abort();
          const spans = memory.getFinishedSpans();
          assert.equal(spans.length, 1);
          assert.equal(spans[0].status.code, SpanStatusCode.UNSET);
          assert.equal(spans[0].attributes["gen_ai.usage.output_tokens"], 0);
          assert(!JSON.stringify(spans[0].attributes).includes("OUTPUT_CANARY"));
        } finally {
          stream.controller.abort();
        }
      });
      await check("rejected second iterator does not finish the active stream", async () => {
        memory.reset();
        const stream = await client.messages.create({ ...params, stream: true });
        const active = stream[Symbol.asyncIterator]();
        const received = [(await active.next()).value];
        assert.equal(received[0].type, "message_start");
        const rejected = stream[Symbol.asyncIterator]();
        await assert.rejects(rejected.next(), /Cannot iterate over a consumed stream/);
        assert.equal(memory.getFinishedSpans().length, 0);
        for (;;) {
          const result = await active.next();
          if (result.done) break;
          received.push(result.value);
        }
        assert.deepEqual(received.map(event => event.type), events.map(event => event.type));
        const spans = memory.getFinishedSpans();
        assert.equal(spans.length, 1);
        const span = spans[0];
        assert.equal(span.status.code, SpanStatusCode.OK);
        assert.equal(span.attributes["gen_ai.usage.input_tokens"], 12);
        assert.equal(span.attributes["gen_ai.usage.output_tokens"], 2);
        assert.equal(span.attributes["gen_ai.usage.total_tokens"], 14);
        assert.equal(span.events.length, 0);
        assert.equal(JSON.stringify(span.attributes).includes("OUTPUT_CANARY"), !privacy);
        // Further operations on the rejected iterator cannot export again.
        await rejected.return();
        assert.equal(memory.getFinishedSpans().length, 1);
      });
    }
    await check("instrumented early break cancels transport", async () => {
      const result = await cancellation("instrumented-cancellation");
      assert.equal(result.aborted, true);
      assert.equal(result.connections, 0);
      assert.equal(result.spans, 1);
      assert.equal(result.status, SpanStatusCode.UNSET);
    });
    await check("typed API error and privacy", async () => {
      memory.reset();
      await assert.rejects(client.messages.create({ ...params, model: "error" }), Anthropic.BadRequestError);
      const spans = memory.getFinishedSpans();
      assert.equal(spans.length, 1);
      const span = spans[0];
      assert.equal(span.status.code, SpanStatusCode.ERROR);
      if (privacy || native) {
        assert(!JSON.stringify({ attributes: span.attributes, status: span.status, events: span.events }).includes("ERROR_CANARY"));
      }
    });
    console.log(JSON.stringify({ sdk, native, privacy, rows, failed }, null, 2));
    process.exitCode = failed.length ? 1 : 0;
  } finally {
    instrumentation.disable();
    await provider.shutdown();
    contextManager.disable();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => {
  console.error(error);
  server.closeAllConnections();
  server.close();
  process.exitCode = 2;
});
