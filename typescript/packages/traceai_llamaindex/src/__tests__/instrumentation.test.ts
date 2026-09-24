import { diag, DiagLogLevel, SpanStatusCode } from "@opentelemetry/api";
import { isWrapped } from "@opentelemetry/instrumentation";
import {
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { FISpanKind, SemanticConventions } from "@traceai/fi-semantic-conventions";
import * as LlamaIndexOpenAI from "@llamaindex/openai";
import * as LlamaIndex from "llamaindex";

import { LlamaIndexInstrumentation } from "../instrumentation";
import {
  FAKE_REPLY,
  FAKE_TOOL_ARGUMENTS,
  FAKE_USAGE,
  FakeOpenAIServer,
  startFakeOpenAIServer,
} from "./fakeOpenAIServer";

const { OpenAI, OpenAIEmbedding } = LlamaIndexOpenAI;

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
provider.register();

const instrumentation = new LlamaIndexInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(LlamaIndex, LlamaIndexOpenAI);

let server: FakeOpenAIServer;

function createLLM(baseURL: string) {
  return new OpenAI({
    model: "gpt-4o-mini",
    apiKey: "sk-test",
    maxRetries: 0,
    additionalSessionOptions: { baseURL },
  });
}

function cityTool(name: string) {
  return {
    metadata: {
      name,
      description: "Look something up for a city",
      parameters: { type: "object", properties: { city: { type: "string" } } },
    },
    call: () => "sunny",
  };
}

const weatherTool = cityTool("get_weather");

function spansOfKind(kind: FISpanKind): ReadableSpan[] {
  return exporter
    .getFinishedSpans()
    .filter((span) => span.attributes[SemanticConventions.FI_SPAN_KIND] === kind);
}

function parseMessages(span: ReadableSpan, key: string) {
  return JSON.parse(String(span.attributes[key]));
}

beforeAll(async () => {
  server = await startFakeOpenAIServer();
  LlamaIndex.Settings.llm = createLLM(server.baseURL);
  LlamaIndex.Settings.embedModel = new OpenAIEmbedding({
    model: "text-embedding-3-small",
    apiKey: "sk-test",
    maxRetries: 0,
    additionalSessionOptions: { baseURL: server.baseURL },
  });
});

afterAll(async () => {
  await server.close();
  await provider.shutdown();
});

beforeEach(() => exporter.reset());

describe("OpenAI chat through @llamaindex/openai", () => {
  it("emits an LLM span for a chat call", async () => {
    const response = await createLLM(server.baseURL).chat({
      messages: [{ role: "user", content: "hi" }],
    });

    expect(response.message.content).toBe(FAKE_REPLY);
    const [span] = spansOfKind(FISpanKind.LLM);
    expect(spansOfKind(FISpanKind.LLM)).toHaveLength(1);
    expect(span.name).toBe("llamaindex.OpenAI.chat");
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("gpt-4o-mini");
    expect(parseMessages(span, SemanticConventions.LLM_OUTPUT_MESSAGES)).toEqual([
      { role: "assistant", content: FAKE_REPLY },
    ]);
  });

  it("names the chat span after a subclass that inherits chat", async () => {
    class AcmeLLM extends OpenAI {}
    const llm = new AcmeLLM({
      model: "gpt-4o-mini",
      apiKey: "sk-test",
      maxRetries: 0,
      additionalSessionOptions: { baseURL: server.baseURL },
    });

    await llm.chat({ messages: [{ role: "user", content: "hi" }] });

    const [span] = spansOfKind(FISpanKind.LLM);
    expect(span.name).toBe("llamaindex.AcmeLLM.chat");
  });

  it("records each input message with its own role", async () => {
    await createLLM(server.baseURL).chat({
      messages: [
        { role: "system", content: "be brief" },
        { role: "user", content: "hi" },
      ],
    });

    const [span] = spansOfKind(FISpanKind.LLM);
    expect(parseMessages(span, SemanticConventions.LLM_INPUT_MESSAGES)).toEqual([
      { role: "system", content: "be brief" },
      { role: "user", content: "hi" },
    ]);
  });

  it("records token counts from the provider usage", async () => {
    await createLLM(server.baseURL).chat({ messages: [{ role: "user", content: "hi" }] });

    const [span] = spansOfKind(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(FAKE_USAGE.prompt_tokens);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(FAKE_USAGE.completion_tokens);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(FAKE_USAGE.total_tokens);
  });

  it("records the tool calls the model asks for", async () => {
    const response = await createLLM(server.baseURL).chat({
      messages: [{ role: "user", content: "weather in Paris?" }],
      tools: [weatherTool],
    });

    expect(response.message.options).toHaveProperty("toolCall");
    const [span] = spansOfKind(FISpanKind.LLM);
    const [output] = parseMessages(span, SemanticConventions.LLM_OUTPUT_MESSAGES);
    expect(output.tool_calls).toEqual([
      {
        id: "call_1",
        type: "function",
        function: { name: "get_weather", arguments: JSON.stringify(FAKE_TOOL_ARGUMENTS) },
      },
    ]);
  });

  it("streams the full text to the caller and records it on the span", async () => {
    const stream = await createLLM(server.baseURL).chat({
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });
    let text = "";
    for await (const chunk of stream) {
      text += chunk.delta;
    }

    expect(text).toBe(FAKE_REPLY);
    const [span] = spansOfKind(FISpanKind.LLM);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(parseMessages(span, SemanticConventions.LLM_OUTPUT_MESSAGES)).toEqual([
      { role: "assistant", content: FAKE_REPLY },
    ]);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL]).toBe(FAKE_USAGE.total_tokens);
  });

  it("records every tool call from a streamed response with parallel tool calls", async () => {
    const stream = await createLLM(server.baseURL).chat({
      messages: [{ role: "user", content: "weather and time in Paris?" }],
      tools: [weatherTool, cityTool("get_time")],
      stream: true,
    });
    for await (const chunk of stream) {
      void chunk;
    }

    const [span] = spansOfKind(FISpanKind.LLM);
    const [output] = parseMessages(span, SemanticConventions.LLM_OUTPUT_MESSAGES);
    const args = JSON.stringify(FAKE_TOOL_ARGUMENTS);
    expect(output.tool_calls).toEqual([
      { id: "call_1", type: "function", function: { name: "get_weather", arguments: args } },
      { id: "call_2", type: "function", function: { name: "get_time", arguments: args } },
    ]);
  });

  it("leaves token counts off a streamed span when the provider sends no usage", async () => {
    const stream = await createLLM(server.noUsageStreamBaseURL).chat({
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });
    let text = "";
    for await (const chunk of stream) {
      text += chunk.delta;
    }

    expect(text).toBe(FAKE_REPLY);
    const [span] = spansOfKind(FISpanKind.LLM);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes).not.toHaveProperty(SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
    expect(span.attributes).not.toHaveProperty(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
    expect(span.attributes).not.toHaveProperty(SemanticConventions.LLM_TOKEN_COUNT_TOTAL);
  });
});

describe("LlamaIndex pipelines", () => {
  it("records retriever, synthesizer, query and LLM spans for a query engine run", async () => {
    const index = await LlamaIndex.VectorStoreIndex.fromDocuments([
      new LlamaIndex.Document({ text: "LlamaIndex is a data framework." }),
    ]);
    exporter.reset();

    const response = await index.asQueryEngine().query({ query: "What is LlamaIndex?" });

    expect(response.toString()).toBe(FAKE_REPLY);
    const names = exporter.getFinishedSpans().map((span) => span.name);
    expect(names).toEqual(
      expect.arrayContaining([
        "OpenAIEmbedding.getQueryEmbedding",
        "VectorIndexRetriever.retrieve",
        "CompactAndRefine.synthesize",
        "RetrieverQueryEngine.query",
        "llamaindex.OpenAI.chat",
      ]),
    );
    expect(spansOfKind(FISpanKind.LLM)).toHaveLength(1);
  });

  it("records a retriever span with the retrieved nodes", async () => {
    const index = await LlamaIndex.VectorStoreIndex.fromDocuments([
      new LlamaIndex.Document({ text: "LlamaIndex is a data framework." }),
    ]);
    exporter.reset();

    const nodes = await index.asRetriever().retrieve({ query: "What is LlamaIndex?" });

    expect(nodes).toHaveLength(1);
    const [span] = spansOfKind(FISpanKind.RETRIEVER);
    expect(span.name).toBe("VectorIndexRetriever.retrieve");
    expect(String(span.attributes[SemanticConventions.OUTPUT_VALUE])).toContain(
      "LlamaIndex is a data framework.",
    );
  });
});

describe("failures", () => {
  it("marks the LLM span as errored, records the exception and still rejects to the caller", async () => {
    const unreachable = createLLM("http://127.0.0.1:1/v1");

    await expect(
      unreachable.chat({ messages: [{ role: "user", content: "hi" }] }),
    ).rejects.toThrow();

    const [span] = spansOfKind(FISpanKind.LLM);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(span.events.map((event) => event.name)).toContain("exception");
  });

  it("marks a streaming span as errored when the stream breaks mid-way", async () => {
    const stream = await createLLM(server.brokenStreamBaseURL).chat({
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });

    await expect(
      (async () => {
        for await (const chunk of stream) {
          void chunk;
        }
      })(),
    ).rejects.toThrow();

    const [span] = spansOfKind(FISpanKind.LLM);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(span.events.map((event) => event.name)).toContain("exception");
  });

  it("does not throw on module shapes without the expected classes and warns instead", () => {
    const warn = jest.fn();
    diag.setLogger(
      { warn, error: jest.fn(), info: jest.fn(), debug: jest.fn(), verbose: jest.fn() },
      DiagLogLevel.WARN,
    );
    const standalone = new LlamaIndexInstrumentation();

    try {
      expect(() =>
        standalone.manuallyInstrument(
          { RetrieverQueryEngine: undefined, ContextChatEngine: {} } as unknown as typeof LlamaIndex,
          { OpenAI: 42, Settings: { llm: null } },
        ),
      ).not.toThrow();
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("@traceai/llamaindex"),
        expect.stringContaining("No LLM classes found"),
      );
    } finally {
      diag.disable();
    }
  });
});

describe("unpatch", () => {
  it("restores every wrapped method, even after patching twice", () => {
    class FakeLLM {
      chat() {}
      complete() {}
    }
    class RetrieverQueryEngine {
      query() {}
      retrieve() {}
    }
    class ContextChatEngine {
      chat() {}
    }
    const moduleExports = { FakeLLM, RetrieverQueryEngine, ContextChatEngine };
    const standalone = new LlamaIndexInstrumentation();
    const wrappedMethods = () => [
      FakeLLM.prototype.chat,
      RetrieverQueryEngine.prototype.query,
      RetrieverQueryEngine.prototype.retrieve,
      ContextChatEngine.prototype.chat,
    ].map(isWrapped);

    standalone["patch"](moduleExports);
    standalone["patch"](moduleExports);
    expect(wrappedMethods()).toEqual([true, true, true, true]);

    standalone["unpatch"](moduleExports);
    expect(wrappedMethods()).toEqual([false, false, false, false]);
  });
});
