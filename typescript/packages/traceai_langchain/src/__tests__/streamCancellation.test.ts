import { createServer, Server } from "http";
import { AddressInfo } from "net";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";
import * as ChatModelModule from "@langchain/core/language_models/chat_models";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";
import { ChatOpenAI } from "@langchain/openai";
import { SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { LangChainInstrumentation } from "../instrumentation";

// Real provider adapter and upstream streaming iterator; only the HTTP service
// is replaced. No provider credentials, network access, or timing-based cleanup.
describe("chat model iterator cancellation", () => {
  let server: Server;
  let baseURL: string;
  beforeAll(async () => {
    server = createServer(async (request, response) => {
      for await (const _part of request) { /* drain the synthetic request */ }
      response.writeHead(200, { "content-type": "text/event-stream" });
      const chunk = { id: "synthetic", object: "chat.completion.chunk", created: 1, model: "gpt-4o-mini" };
      response.write(`data: ${JSON.stringify({ ...chunk, choices: [{ index: 0, delta: { role: "assistant", content: "synthetic-private-answer" }, finish_reason: null }] })}\n\n`);
      // Intentionally leave the response open until the consumer returns its
      // iterator. A timeout-based span reaper could not pass this assertion.
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
  });
  afterAll(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  it.each([false, true])("ends an early return exactly once (hideOutputs=%s)", async (hidden) => {
    const exporter = new InMemorySpanExporter();
    let started = 0;
    let ended = 0;
    const provider = new BasicTracerProvider({ spanProcessors: [
      { onStart() { started++; }, onEnd() { ended++; }, async forceFlush() {}, async shutdown() {} },
      new SimpleSpanProcessor(exporter),
    ] });
    const instrumentation = new LangChainInstrumentation({ tracerProvider: provider, traceConfig: { hideOutputs: hidden } });
    instrumentation.manuallyInstrument(CallbackManagerModule, ChatModelModule);
    try {
      const model = new ChatOpenAI({ apiKey: "synthetic-key", model: "gpt-4o-mini", maxRetries: 0, configuration: { baseURL } });
      const stream = await model.stream("synthetic-query");
      for await (const chunk of stream) {
        expect(chunk.content).toBe("synthetic-private-answer");
        break;
      }
      // Repeated closure cannot create another end event.
      await stream.return();
      await awaitAllCallbacks();
      await provider.forceFlush();
      expect(started).toBe(1);
      expect(ended).toBe(1);
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
      expect(spans[0].attributes["langchain.stream.cancelled"]).toBe(true);
      expect(spans[0].attributes["gen_ai.usage.total_tokens"]).toBeUndefined();
      expect(JSON.stringify(spans[0].attributes)).not.toContain("synthetic-private-answer");
    } finally {
      instrumentation.disable();
      await provider.shutdown();
    }
  });
});
