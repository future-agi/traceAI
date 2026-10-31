import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { CerebrasInstrumentation, isPatched } from "../instrumentation";

// CI runs the same contract against separately installed floor/current SDKs.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const sdk = require(
  process.env.CEREBRAS_SDK_PATH || "@cerebras/cerebras_cloud_sdk",
);
const Cerebras = sdk.Cerebras;
const request = {
  model: "test-model",
  messages: [{ role: "user", content: "private-input" }],
};
const usage = { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 };
const timing = {
  queue_time: 0.01,
  prompt_time: 0.02,
  completion_time: 0.03,
  total_time: 0.06,
};
const response = {
  id: "completion-1",
  object: "chat.completion",
  created: 1,
  model: "resolved-model",
  choices: [
    {
      index: 0,
      message: { role: "assistant", content: "private-output" },
      finish_reason: "stop",
    },
  ],
  usage,
  time_info: timing,
};
const chunks = [
  {
    id: "completion-1",
    model: "resolved-model",
    choices: [
      {
        index: 0,
        delta: { role: "assistant", content: "hello" },
        finish_reason: null,
      },
    ],
  },
  {
    id: "completion-1",
    model: "resolved-model",
    choices: [
      { index: 0, delta: { content: " world" }, finish_reason: "stop" },
    ],
    usage,
    time_info: timing,
  },
  // An empty trailing chunk must not discard usage/timing from earlier chunks.
  { id: "completion-1", model: "resolved-model", choices: [] },
];
function jsonResponse(body: unknown = response, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      "x-request-id": "request-1",
    },
  });
}
function sseResponse(items: unknown[] = chunks) {
  return new Response(
    items.map((item) => `data: ${JSON.stringify(item)}\n\n`).join("") +
      "data: [DONE]\n\n",
    { headers: { "content-type": "text/event-stream" } },
  );
}
function client(
  fetch = jest.fn(async (_input?: unknown, _init?: RequestInit) =>
    jsonResponse(),
  ),
) {
  return {
    fetch,
    client: new Cerebras({
      apiKey: "offline-test-key",
      baseURL: "http://127.0.0.1:9",
      warmTCPConnection: false,
      maxRetries: 0,
      fetch,
    }),
  };
}
async function collect(stream: AsyncIterable<unknown>) {
  const result = [];
  for await (const chunk of stream) result.push(chunk);
  return result;
}

describe("real Cerebras SDK resource contract", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  let instrumentation: CerebrasInstrumentation;
  beforeAll(() => provider.register());
  beforeEach(() => {
    instrumentation = new CerebrasInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.manuallyInstrument(sdk);
  });
  afterEach(() => {
    instrumentation.disable();
    exporter.reset();
  });
  afterAll(async () => {
    await provider.shutdown();
    context.disable();
    trace.disable();
  });

  it("patches the real resource and traces a successful request", async () => {
    const c = client();
    expect(Cerebras.prototype.chat).toBeUndefined();
    expect(await c.client.chat.completions.create(request)).toEqual(response);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes).toMatchObject({
      [S.LLM_PROVIDER]: "cerebras",
      [S.LLM_MODEL_NAME]: "test-model",
      [S.GEN_AI_RESPONSE_MODEL]: "resolved-model",
      [S.GEN_AI_OPERATION_NAME]: "chat",
      [S.GEN_AI_RESPONSE_ID]: "completion-1",
      [S.LLM_TOKEN_COUNT_TOTAL]: 10,
      "cerebras.total_time": 0.06,
    });
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("preserves APIPromise helpers and repeated awaits without duplicate spans", async () => {
    const pending = client().client.chat.completions.create(request);
    expect(pending).toBeInstanceOf(Promise);
    expect(pending.constructor).toBe(
      Object.getPrototypeOf(pending).constructor,
    );
    expect(typeof pending.asResponse).toBe("function");
    expect(typeof pending.withResponse).toBe("function");
    const result = await pending.withResponse();
    expect(result.data).toEqual(response);
    expect(result.response.headers.get("x-request-id")).toBe("request-1");
    expect(await pending).toBe(result.data);
    expect(await pending.finally(() => undefined)).toBe(result.data);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("does not consume the body for asResponse", async () => {
    const raw = await client()
      .client.chat.completions.create(request)
      .asResponse();
    expect(raw.bodyUsed).toBe(false);
    expect(await raw.json()).toEqual(response);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(
      exporter.getFinishedSpans()[0].attributes[S.OUTPUT_VALUE],
    ).toBeUndefined();
  });

  it("forwards options and parents the request span", async () => {
    const c = client();
    const parent = provider.getTracer("test").startSpan("parent");
    await context.with(trace.setSpan(context.active(), parent), () =>
      c.client.chat.completions.create(request, {
        headers: { "x-offline-option": "kept" },
      }),
    );
    expect(
      new Headers(c.fetch.mock.calls[0][1]!.headers).get("x-offline-option"),
    ).toBe("kept");
    expect(exporter.getFinishedSpans()[0].parentSpanId).toBe(
      parent.spanContext().spanId,
    );
    parent.end();
  });

  it("respects OpenTelemetry suppression", async () => {
    await context.with(suppressTracing(context.active()), () =>
      client().client.chat.completions.create(request),
    );
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("does not double-patch and disable restores existing clients", async () => {
    const c = client();
    instrumentation.manuallyInstrument(sdk);
    await c.client.chat.completions.create(request);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    instrumentation.disable();
    expect(isPatched()).toBe(false);
    await c.client.chat.completions.create(request);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    instrumentation.enable();
    await c.client.chat.completions.create(request);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("does not report an unsupported module as patched", () => {
    instrumentation.disable();
    instrumentation = new CerebrasInstrumentation({
      instrumentationConfig: { enabled: false },
    });
    instrumentation.manuallyInstrument({ Cerebras: class {} });
    expect(isPatched()).toBe(false);
  });

  it("ends rejected requests and preserves SDK errors", async () => {
    const c = client(
      jest.fn(async () =>
        jsonResponse({ error: { message: "offline-error" } }, 400),
      ),
    );
    await expect(
      c.client.chat.completions.create(request).withResponse(),
    ).rejects.toMatchObject({ status: 400 });
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(
      SpanStatusCode.ERROR,
    );
  });

  it("streams lazily with the SDK controller and methods intact", async () => {
    const c = client(jest.fn(async () => sseResponse()));
    const pending = c.client.chat.completions.create({
      ...request,
      stream: true,
    });
    const { data: stream } = await pending.withResponse();
    expect(typeof stream.tee).toBe("function");
    expect(typeof stream.toReadableStream).toBe("function");
    expect(stream.controller).toBeInstanceOf(AbortController);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(await collect(stream)).toEqual(chunks);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes).toMatchObject({
      [S.OUTPUT_VALUE]: "hello world",
      [S.LLM_TOKEN_COUNT_TOTAL]: 10,
      "cerebras.total_time": 0.06,
    });
  });

  it("keeps tee branches and readable-stream conversion working", async () => {
    const stream = await client(
      jest.fn(async () => sseResponse()),
    ).client.chat.completions.create({ ...request, stream: true });
    const [a, b] = stream.tee();
    expect(await collect(a)).toEqual(chunks);
    expect(await collect(b)).toEqual(chunks);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const stream2 = await client(
      jest.fn(async () => sseResponse()),
    ).client.chat.completions.create({ ...request, stream: true });
    const text = await new Response(stream2.toReadableStream()).text();
    expect(
      text
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line)),
    ).toEqual(chunks);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("ends partial streams on break and abort", async () => {
    const stream = await client(
      jest.fn(async () => sseResponse()),
    ).client.chat.completions.create({ ...request, stream: true });
    for await (const _ of stream) break;
    expect(stream.controller.signal.aborted).toBe(true);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    const other = await client(
      jest.fn(async () => sseResponse()),
    ).client.chat.completions.create({ ...request, stream: true });
    other.controller.abort();
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("records stream failures without swallowing the SDK error", async () => {
    const stream = await client(
      jest.fn(async () =>
        sseResponse([
          chunks[0],
          { error: { message: "offline-stream-error" } },
        ]),
      ),
    ).client.chat.completions.create({ ...request, stream: true });
    await expect(collect(stream)).rejects.toThrow();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(
      SpanStatusCode.ERROR,
    );
  });

  it("does not change unexpected but SDK-accepted responses", async () => {
    for (const value of [
      { choices: null },
      { choices: "unexpected" },
      "text",
    ]) {
      expect(
        await client(
          jest.fn(async () => jsonResponse(value)),
        ).client.chat.completions.create(request),
      ).toEqual(value);
    }
    expect(exporter.getFinishedSpans()).toHaveLength(3);
  });

  it("keeps the raw-response rejection and ends its span", async () => {
    const c = client(
      jest.fn(async () =>
        jsonResponse({ error: { message: "offline-error" } }, 400),
      ),
    );
    await expect(
      c.client.chat.completions.create(request).asResponse(),
    ).rejects.toMatchObject({ status: 400 });
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(
      SpanStatusCode.ERROR,
    );
  });

  const textTest = Cerebras.Completions ? it : it.skip;
  textTest(
    "retains streamed text, usage and timing for text completions",
    async () => {
      const textChunks = [
        {
          id: "text-1",
          model: "resolved-model",
          choices: [{ index: 0, text: "hello", finish_reason: null }],
        },
        {
          id: "text-1",
          model: "resolved-model",
          choices: [{ index: 0, text: " world", finish_reason: "stop" }],
          usage,
          time_info: timing,
        },
      ];
      const stream = await client(
        jest.fn(async () => sseResponse(textChunks)),
      ).client.completions.create({
        model: "test-model",
        prompt: "hello",
        stream: true,
      });
      expect(await collect(stream)).toEqual(textChunks);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exporter.getFinishedSpans()[0].attributes).toMatchObject({
        [S.OUTPUT_VALUE]: "hello world",
        [S.LLM_TOKEN_COUNT_TOTAL]: 10,
        [S.GEN_AI_OPERATION_NAME]: "text_completion",
        "cerebras.total_time": 0.06,
      });
    },
  );
  textTest(
    "traces native text completions when supported by the SDK",
    async () => {
      const text = {
        ...response,
        choices: [{ index: 0, text: "private-output", finish_reason: "stop" }],
      };
      const c = client(jest.fn(async () => jsonResponse(text)));
      expect(
        await c.client.completions.create({
          model: "test-model",
          prompt: "private-input",
        }),
      ).toEqual(text);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(
        exporter.getFinishedSpans()[0].attributes[S.GEN_AI_OPERATION_NAME],
      ).toBe("text_completion");
    },
  );
});
