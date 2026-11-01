// Real OpenAI resources, real FITracer, and real localhost OTLP/HTTP export.
const assert = require("node:assert/strict");
const { test, before, after, beforeEach, afterEach } = require("node:test");
const { createServer } = require("node:http");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");
const path = require("node:path");
const { VLLMInstrumentation, isPatched } = require(
  process.env.VLLM_BUILD || "../dist/cjs",
);
const { context, trace, SpanStatusCode } = require("@opentelemetry/api");
const { suppressTracing } = require("@opentelemetry/core");
const coreRequire = createRequire(require.resolve("@traceai/fi-core"));
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } =
  coreRequire("@opentelemetry/sdk-trace-base");
const { OTLPTraceExporter } = coreRequire(
  "@opentelemetry/exporter-trace-otlp-http",
);
const { AsyncLocalStorageContextManager } = coreRequire(
  "@opentelemetry/context-async-hooks",
);
const sdkPath = process.env.OPENAI_MODULE || require.resolve("openai");
const sdk = require(sdkPath);
const OpenAI = sdk.OpenAI;
const input = "SYNTHETIC_INPUT_MARKER";
const output = "SYNTHETIC_OUTPUT_MARKER";
const secret = "SYNTHETIC_CREDENTIAL_MARKER";
const body = (chat, extras = {}) => ({
  model: "served-model",
  ...(chat
    ? { messages: [{ role: "user", content: input }] }
    : { prompt: input }),
  ...extras,
});
const response = (chat, toolArgument = '{"x":1}') => ({
  id: "response-id",
  object: chat ? "chat.completion" : "text_completion",
  created: 0,
  model: "served-model",
  choices: [
    {
      index: 0,
      ...(chat
        ? {
            message: {
              role: "assistant",
              content: output,
              tool_calls: [
                {
                  id: "tool-id",
                  type: "function",
                  function: { name: "lookup", arguments: toolArgument },
                },
              ],
            },
          }
        : { text: output }),
      finish_reason: "stop",
    },
  ],
  usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 },
});
const chunks = (chat) => [
  {
    id: "response-id",
    model: "served-model",
    choices: [
      {
        index: 0,
        ...(chat
          ? {
              delta: {
                content: output,
                tool_calls: [
                  {
                    index: 0,
                    id: "tool-id",
                    type: "function",
                    function: { name: "lookup", arguments: '{"x":' },
                  },
                ],
              },
            }
          : { text: output }),
        finish_reason: null,
      },
    ],
  },
  {
    id: "response-id",
    model: "served-model",
    choices: [
      {
        index: 0,
        ...(chat
          ? {
              delta: {
                tool_calls: [{ index: 0, function: { arguments: "1}" } }],
              },
            }
          : { text: "" }),
        finish_reason: "stop",
      },
    ],
  },
  {
    id: "response-id",
    model: "served-model",
    choices: [],
    usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 },
  },
];
let provider, server, inst, packets, mem, calls, reads;
function instrument(config = {}, module = sdk) {
  inst = new VLLMInstrumentation({
    baseUrlPattern: /^http:\/\/vllm\.test(?::\d+)?\/v1\/?$/,
    ...config,
  });
  inst.setTracerProvider(provider);
  inst.manuallyInstrument(module);
  return inst;
}
function client(
  chat,
  {
    stream = false,
    status = 200,
    malformed = false,
    streamError = false,
    transportError = false,
    privateToolArgument = false,
    baseURL = "http://vllm.test/v1",
  } = {},
) {
  return new OpenAI({
    apiKey: secret,
    baseURL,
    maxRetries: 0,
    fetch: async () => {
      calls++;
      if (transportError) throw new Error(input);
      const payload = stream
        ? chunks(chat)
            .map((c) => `data: ${JSON.stringify(c)}\n\n`)
            .join("") +
          (streamError ? "data: {invalid-json\n\n" : "data: [DONE]\n\n")
        : malformed
          ? "{bad-json"
          : JSON.stringify(
              status >= 400
                ? { error: { message: input } }
                : response(chat, privateToolArgument ? output : '{"x":1}'),
            );
      const result = new Response(payload, {
        status,
        headers: {
          "content-type": stream ? "text/event-stream" : "application/json",
          "x-request-id": "request-id",
        },
      });
      const json = result.json.bind(result);
      const text = result.text.bind(result);
      result.json = () => {
        reads++;
        return json();
      };
      result.text = () => {
        reads++;
        return text();
      };
      return result;
    },
  });
}
function create(client, chat, extras) {
  return (chat ? client.chat.completions : client.completions).create(
    body(chat, extras),
  );
}
async function wire(expected = 1) {
  await provider.forceFlush();
  const spans = packets
    .flatMap((p) => p.resourceSpans ?? [])
    .flatMap((r) => r.scopeSpans ?? [])
    .flatMap((s) => s.spans ?? []);
  assert.equal(spans.length, expected, "OTLP exported span count");
  assert.ok(
    !JSON.stringify(packets).includes(secret),
    "credential never exported",
  );
  return spans.map((span) => ({
    ...span,
    attrs: Object.fromEntries(
      span.attributes.map((a) => [
        a.key,
        a.value.stringValue ??
          a.value.intValue ??
          a.value.doubleValue ??
          a.value.boolValue,
      ]),
    ),
  }));
}
async function one(expectedStatus = SpanStatusCode.OK) {
  const [span] = await wire();
  assert.equal(span.attrs["gen_ai.provider.name"], "vllm");
  assert.equal(span.attrs["gen_ai.request.model"], "served-model");
  assert.equal(span.status?.code ?? 0, expectedStatus);
  return span;
}
before(async () => {
  packets = [];
  server = createServer((req, res) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
    });
    req.on("end", () => {
      packets.push(JSON.parse(data));
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  mem = new InMemorySpanExporter();
  provider = new BasicTracerProvider({
    spanProcessors: [
      new SimpleSpanProcessor(mem),
      new SimpleSpanProcessor(
        new OTLPTraceExporter({
          url: `http://127.0.0.1:${server.address().port}/v1/traces`,
        }),
      ),
    ],
  });
  trace.setGlobalTracerProvider(provider);
  context.setGlobalContextManager(
    new AsyncLocalStorageContextManager().enable(),
  );
});
beforeEach(() => {
  packets = [];
  mem.reset();
  calls = 0;
  reads = 0;
});
afterEach(async () => {
  inst?.disable();
  inst = undefined;
  await provider.forceFlush();
});
after(async () => {
  await provider.shutdown();
  await new Promise((resolve) => server.close(resolve));
  trace.disable();
  context.disable();
});

for (const chat of [true, false]) {
  const name = chat ? "chat" : "completion";
  test(`${name}: actual resource, usage and one wire span`, async () => {
    instrument();
    const c = client(chat);
    const result = await create(c, chat);
    assert.equal(result.id, "response-id");
    assert.equal(calls, 1);
    const span = await one();
    assert.equal(span.attrs["gen_ai.usage.input_tokens"], 5);
    assert.equal(span.attrs["gen_ai.usage.output_tokens"], 3);
    assert.equal(span.attrs["gen_ai.response.model"], "served-model");
    assert.equal(span.kind, 3, "OTLP CLIENT span kind");
    assert.equal(
      span.attrs["gen_ai.operation.name"],
      chat ? "chat" : "text_completion",
    );
    assert.ok(JSON.stringify(span.attrs).includes(input));
    assert.ok(JSON.stringify(span.attrs).includes(output));
  });
  test(`${name}: APIPromise remains lazy and withResponse preserves data identity`, async () => {
    instrument();
    const promise = create(client(chat), chat);
    assert.equal(typeof promise.asResponse, "function");
    assert.equal(typeof promise.withResponse, "function");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(reads, 0);
    const pair = await promise.withResponse();
    assert.equal(pair.data, await promise);
    assert.equal(pair.response.headers.get("x-request-id"), "request-id");
    assert.equal(reads, 1);
    await one();
  });
  test(`${name}: raw response body is untouched`, async () => {
    instrument();
    const promise = create(client(chat), chat);
    const raw = await promise.asResponse();
    assert.equal(raw.bodyUsed, false);
    assert.equal(reads, 0);
    assert.equal((await raw.json()).id, "response-id");
    const span = await one();
    assert.equal(span.attrs["output.value"], undefined);
  });
  for (const mode of ["http", "transport", "parse"]) {
    test(`${name}: ${mode} rejection exports exactly once`, async () => {
      instrument({ traceConfig: { hideInputs: true, hideOutputs: true } });
      const promise = create(
        client(chat, {
          status: mode === "http" ? 400 : 200,
          transportError: mode === "transport",
          malformed: mode === "parse",
        }),
        chat,
      );
      await assert.rejects(promise);
      await one(SpanStatusCode.ERROR);
      assert.ok(!JSON.stringify(packets).includes(input));
    });
  }
  test(`${name}: caller catch recovery keeps an error span`, async () => {
    instrument();
    const result = await create(client(chat, { malformed: true }), chat).catch(
      () => "recovered",
    );
    assert.equal(result, "recovered");
    await one(SpanStatusCode.ERROR);
  });
  test(`${name}: original stream and helpers, full consumption`, async () => {
    instrument();
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    assert.ok(stream.controller instanceof AbortController);
    assert.equal(
      mem.getFinishedSpans().length,
      0,
      "span stays open while streaming",
    );
    const seen = [];
    for await (const chunk of stream) seen.push(chunk);
    assert.deepEqual(seen, chunks(chat));
    const span = await one();
    assert.equal(span.attrs["output.value"], output);
    assert.equal(span.attrs["gen_ai.usage.input_tokens"], 5);
    assert.equal(span.attrs["gen_ai.usage.output_tokens"], 3);
    if (chat)
      assert.equal(
        JSON.parse(span.attrs["gen_ai.output.messages"])[0].tool_calls[0]
          .function.arguments,
        '{"x":1}',
      );
  });
  test(`${name}: the returned iterator remains async iterable`, async () => {
    instrument();
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    const seen = [];
    for await (const chunk of stream[Symbol.asyncIterator]()) seen.push(chunk);
    assert.deepEqual(seen, chunks(chat));
    await one();
  });
  test(`${name}: iterator throw preserves the original exception`, async () => {
    instrument();
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    const iterator = stream[Symbol.asyncIterator]();
    await iterator.next();
    const error = new Error("synthetic iterator failure");
    await assert.rejects(iterator.throw(error), (value) => value === error);
    await one(SpanStatusCode.ERROR);
  });
  test(`${name}: stream break closes without a false success`, async () => {
    instrument();
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    for await (const chunk of stream) {
      assert.ok(chunk);
      break;
    }
    assert.equal(stream.controller.signal.aborted, true);
    await one(SpanStatusCode.UNSET);
  });
  test(`${name}: stream return before first next closes`, async () => {
    instrument();
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    await stream[Symbol.asyncIterator]().return();
    await one(SpanStatusCode.UNSET);
  });
  test(`${name}: explicit abort without iteration closes`, async () => {
    instrument();
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    stream.controller.abort();
    await one(SpanStatusCode.UNSET);
  });
  test(`${name}: stream parse error remains an error`, async () => {
    instrument();
    const stream = await create(
      client(chat, { stream: true, streamError: true }),
      chat,
      { stream: true },
    );
    await assert.rejects(async () => {
      for await (const chunk of stream) void chunk;
    });
    await one(SpanStatusCode.ERROR);
  });
  test(`${name}: raw SSE never starts a telemetry reader`, async () => {
    instrument();
    const raw = await create(client(chat, { stream: true }), chat, {
      stream: true,
    }).asResponse();
    assert.equal(raw.bodyUsed, false);
    assert.ok((await raw.text()).includes(output));
    await one();
  });
  test(`${name}: configured privacy reaches OTLP, including prompt extras/tools`, async () => {
    instrument({ traceConfig: { hideInputs: true, hideOutputs: true } });
    await create(client(chat), chat, {
      tools: [
        { type: "function", function: { name: "lookup", description: input } },
      ],
      extra_body: { template: input },
    });
    await one();
    assert.ok(!JSON.stringify(packets).includes(input));
    assert.ok(!JSON.stringify(packets).includes(output));
  });
  test(`${name}: privacy covers streaming output`, async () => {
    instrument({ traceConfig: { hideInputs: true, hideOutputs: true } });
    const stream = await create(client(chat, { stream: true }), chat, {
      stream: true,
    });
    for await (const chunk of stream) void chunk;
    await one();
    assert.ok(!JSON.stringify(packets).includes(input));
    assert.ok(!JSON.stringify(packets).includes(output));
  });
  for (const flag of ["hideInputMessages", "hideInputText", "hideInputImages"]) {
    test(`${name}: ${flag} hides all request payload aliases on OTLP`, async () => {
      instrument({ traceConfig: { [flag]: true } });
      await create(client(chat), chat, {
        tools: [
          { type: "function", function: { name: "lookup", description: input } },
        ],
        extra_body: { template: input },
      });
      const span = await one();
      assert.ok(!JSON.stringify(packets).includes(input));
      assert.ok(
        JSON.stringify(span.attrs).includes(output),
        "output remains visible",
      );
      for (const key of [
        "input.value",
        "raw.input",
        "gen_ai.input.messages",
        "gen_ai.request.parameters",
        "gen_ai.tool.definitions",
      ])
        assert.equal(span.attrs[key], undefined, `${key} must not bypass privacy`);
    });
  }
  for (const flag of ["hideOutputMessages", "hideOutputText"]) {
    for (const stream of [false, true]) {
      test(`${name}: ${flag} hides ${stream ? "streaming" : "regular"} response payload aliases on OTLP`, async () => {
        instrument({ traceConfig: { [flag]: true } });
        const result = await create(
          client(chat, { stream, privateToolArgument: true }),
          chat,
          stream ? { stream: true } : {},
        );
        if (stream) for await (const chunk of result) void chunk;
        const span = await one();
        assert.ok(!JSON.stringify(packets).includes(output));
        assert.ok(
          JSON.stringify(span.attrs).includes(input),
          "input remains visible",
        );
        for (const key of [
          "output.value",
          "raw.output",
          "gen_ai.output.messages",
        ])
          assert.equal(span.attrs[key], undefined, `${key} must not bypass privacy`);
      });
    }
  }
}

test("documented FI register setup sends private spans to the configured endpoint", async () => {
  const { register, ProjectType } = require("@traceai/fi-core");
  const documentedProvider = register({
    projectName: "vllm-client",
    projectType: ProjectType.OBSERVE,
    endpoint: `http://127.0.0.1:${server.address().port}/v1/traces`,
    headers: { "x-api-key": "synthetic", "x-secret-key": "synthetic" },
    setGlobalTracerProvider: false,
  });
  try {
    instrument({ traceConfig: { hideInputs: true, hideOutputs: true } });
    inst.setTracerProvider(documentedProvider);
    await create(client(true), true);
    await documentedProvider.forceFlush();
    await one();
    assert.ok(!JSON.stringify(packets).includes(input));
    assert.ok(!JSON.stringify(packets).includes(output));
  } finally {
    await documentedProvider.shutdown();
  }
});

test("zero resource targets are not reported as patched", () => {
  instrument({}, { OpenAI: class Empty {} });
  assert.equal(isPatched(), false);
});
test("filter reads actual SDK client; non-vLLM has no span", async () => {
  instrument();
  await create(client(true, { baseURL: "http://other.test/v1" }), true);
  assert.equal(calls, 1);
  await wire(0);
});
test("global regexp does not skip alternate requests", async () => {
  const pattern = /vllm\.test/g;
  pattern.lastIndex = 100;
  instrument({ baseUrlPattern: pattern });
  const c = client(true);
  for (let i = 0; i < 3; i++) await create(c, true);
  await wire(3);
  assert.equal(pattern.lastIndex, 100);
});
test("duplicate manual patch and immutable namespace export are safe", async () => {
  instrument({}, Object.freeze({ OpenAI }));
  inst.manuallyInstrument(sdk);
  inst.manuallyInstrument(sdk);
  await create(client(true), true);
  await one();
});
test("disable restores actual resource methods and invalidates saved wrappers", async () => {
  const original = OpenAI.Chat.Completions.prototype.create;
  instrument();
  const c = client(true);
  const saved = c.chat.completions.create;
  assert.notEqual(saved, original);
  inst.disable();
  assert.equal(c.chat.completions.create, original);
  assert.equal(isPatched(), false);
  await saved.call(c.chat.completions, body(true));
  await wire(0);
  inst.enable();
  await create(c, true);
  await one();
});
test("non-owning instrumentor cannot remove existing wrappers", async () => {
  instrument();
  const second = new VLLMInstrumentation();
  second.setTracerProvider(provider);
  second.manuallyInstrument(sdk);
  second.disable();
  await create(client(true), true);
  await one();
});
test("disabled construction waits for enable and supports manual registration", async () => {
  instrument({ instrumentationConfig: { enabled: false } });
  await create(client(true), true);
  await wire(0);
  inst.enable();
  await create(client(true), true);
  await one();
});
test("suppression produces no client span", async () => {
  instrument();
  await context.with(suppressTracing(context.active()), () =>
    create(client(true), true),
  );
  await wire(0);
});
test("span is parented to the caller context", async () => {
  instrument();
  const parent = provider.getTracer("test").startSpan("parent");
  await context.with(trace.setSpan(context.active(), parent), () =>
    create(client(true), true),
  );
  const span = await one();
  assert.equal(span.parentSpanId, parent.spanContext().spanId);
  parent.end();
});
test("provider replacement after patch takes effect", async () => {
  instrument();
  const replacement = new InMemorySpanExporter();
  const replacementProvider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(replacement)],
  });
  inst.setTracerProvider(replacementProvider);
  await create(client(true), true);
  assert.equal(replacement.getFinishedSpans().length, 1);
  await wire(0);
  await replacementProvider.shutdown();
});
test("real ESM namespace is manually instrumented", async () => {
  const esmPath = path.join(path.dirname(sdkPath), "index.mjs");
  const esm = await import(pathToFileURL(esmPath).href);
  instrument({}, esm);
  const c = new esm.OpenAI({
    apiKey: secret,
    baseURL: "http://vllm.test/v1",
    fetch: async () =>
      new Response(JSON.stringify(response(true)), {
        headers: { "content-type": "application/json" },
      }),
  });
  await create(c, true);
  await one();
});
test("tee preserves native branch behavior when SDK supports it", async () => {
  instrument();
  const stream = await create(client(true, { stream: true }), true, {
    stream: true,
  });
  if (typeof stream.tee !== "function") {
    for await (const chunk of stream) void chunk;
  } else {
    const [a, b] = stream.tee();
    const consume = async (branch) => {
      const result = [];
      for await (const chunk of branch) result.push(chunk);
      return result;
    };
    const [left, right] = await Promise.all([consume(a), consume(b)]);
    assert.deepEqual(left, right);
  }
  await one();
});
