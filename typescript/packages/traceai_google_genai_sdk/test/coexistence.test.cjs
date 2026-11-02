const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { createRequire } = require("node:module");
const req = createRequire(path.join(process.env.CONSUMER_DIR, "package.json"));

test("published legacy and modern adapters trace their own clients exactly once", async () => {
  const { instrumentGoogleGenAI } = req("@traceai/google-genai-sdk");
  const { GoogleGenAIInstrumentation } = req("@traceai/google-genai");
  const google = req("@google/generative-ai");
  const { GoogleGenAI } = req("@google/genai");
  const { NodeTracerProvider } = req("@opentelemetry/sdk-trace-node");
  const { SimpleSpanProcessor, InMemorySpanExporter } = req(
    "@opentelemetry/sdk-trace-base",
  );
  const memory = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memory)],
  });
  const server = http.createServer(async (request, response) => {
    for await (const chunk of request) {
    }
    response.setHeader("content-type", "application/json");
    response.end(
      JSON.stringify({
        candidates: [
          {
            content: { role: "model", parts: [{ text: "answer" }] },
            finishReason: "STOP",
          },
        ],
      }),
    );
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const legacy = new GoogleGenAIInstrumentation({
    instrumentationConfig: { enabled: false },
  });
  legacy.setTracerProvider(provider);
  legacy.manuallyInstrument(google);
  const modern = new GoogleGenAI({
    apiKey: "synthetic-modern",
    httpOptions: { baseUrl },
  });
  const handle = instrumentGoogleGenAI(modern, { tracerProvider: provider });
  try {
    const old = new google.GoogleGenerativeAI(
      "synthetic-legacy",
    ).getGenerativeModel({ model: "gemini-test" }, { baseUrl });
    const [a, b] = await Promise.all([
      old.generateContent("legacy"),
      modern.models.generateContent({
        model: "gemini-test",
        contents: "modern",
      }),
    ]);
    assert.equal(a.response.text(), "answer");
    assert.equal(b.text, "answer");
    assert.equal(memory.getFinishedSpans().length, 2);
    assert.equal(
      memory
        .getFinishedSpans()
        .filter(
          (s) => s.instrumentationScope.name === "@traceai/google-genai-sdk",
        ).length,
      1,
    );
    assert.equal(
      memory
        .getFinishedSpans()
        .filter((s) => s.name === "Google GenAI generateContent").length,
      1,
    );
  } finally {
    handle.dispose();
    legacy.disable();
    await provider.shutdown();
    await new Promise((resolve) => server.close(resolve));
  }
});
