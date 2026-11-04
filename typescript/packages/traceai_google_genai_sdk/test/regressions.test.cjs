const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { createRequire } = require("node:module");
const path = require("node:path");
const req = process.env.CONSUMER_DIR
  ? createRequire(path.join(process.env.CONSUMER_DIR, "package.json"))
  : require;
const { GoogleGenAI } = req("@google/genai");
const { instrumentGoogleGenAI } = process.env.CONSUMER_DIR
  ? req("@traceai/google-genai-sdk")
  : require("../dist");
const { NodeTracerProvider } = req("@opentelemetry/sdk-trace-node");
const { SimpleSpanProcessor, InMemorySpanExporter } = req(
  "@opentelemetry/sdk-trace-base",
);
const { OTLPTraceExporter } = req("@opentelemetry/exporter-trace-otlp-http");
const memory = new InMemorySpanExporter();
let server,
  baseUrl,
  provider,
  handler,
  requests = [],
  wire = [];
const json = (res, body) => {
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const sse = (res, events) => {
  res.writeHead(200, { "content-type": "text/event-stream" });
  for (const e of events) res.write(`data: ${JSON.stringify(e)}\n\n`);
  res.end();
};
const answer = (
  text,
  usage = { promptTokenCount: 4, candidatesTokenCount: 2, totalTokenCount: 6 },
) => ({
  candidates: [
    {
      index: 0,
      content: { role: "model", parts: [{ text }] },
      finishReason: "STOP",
    },
  ],
  usageMetadata: usage,
});
const client = () =>
  new GoogleGenAI({
    apiKey: "synthetic-google-key",
    httpOptions: { baseUrl, retryOptions: { attempts: 1 } },
  });
const spans = () => memory.getFinishedSpans();
function reset() {
  memory.reset();
  requests = [];
  wire = [];
}
function attrs() {
  assert.equal(spans().length, 1);
  return spans()[0].attributes;
}
async function exported() {
  await provider.forceFlush();
  const all = wire.flatMap((b) =>
    b.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
  );
  assert.equal(all.length, 1);
  return Object.fromEntries(
    all[0].attributes.map((a) => [
      a.key,
      a.value.stringValue ?? a.value.intValue ?? a.value.boolValue,
    ]),
  );
}
before(async () => {
  server = http.createServer(async (req, res) => {
    // SDK responses expose transport headers; keep baseline comparisons independent of wall time.
    res.sendDate = false;
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    if (req.url === "/v1/traces") {
      wire.push(body);
      return json(res, {});
    }
    requests.push(body);
    handler(req, res);
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  provider = new NodeTracerProvider({
    spanProcessors: [
      new SimpleSpanProcessor(memory),
      new SimpleSpanProcessor(
        new OTLPTraceExporter({ url: baseUrl + "/v1/traces" }),
      ),
    ],
  });
  provider.register();
});
after(async () => {
  await provider.shutdown();
  server.closeAllConnections();
  await new Promise((r) => server.close(r));
});

// Current payloads mirror @google/genai 2.32.0 Interactions.Interaction / InteractionSSEEvent.
// Legacy outputs/content.delta contracts are exercised separately by the compatibility suite.
test("released Interactions model_output steps reach actual OTLP", async () => {
  reset();
  const ai = client();
  const h = instrumentGoogleGenAI(ai);
  const body = {
    id: "i-current",
    model: "gemini-test",
    status: "completed",
    steps: [
      {
        type: "user_input",
        content: [{ type: "text", text: "NOT_MODEL_OUTPUT" }],
      },
      {
        type: "model_output",
        content: [{ type: "text", text: "CURRENT_OUTPUT_SENTINEL" }],
      },
    ],
  };
  handler = (_, res) => json(res, body);
  try {
    const response = await ai.interactions.create({
      model: "gemini-test",
      input: "hello",
      automatic_continuation: false,
    });
    assert.deepEqual(response.steps, body.steps);
    assert.match(
      attrs()["gen_ai.output.messages"] || "",
      /CURRENT_OUTPUT_SENTINEL/,
    );
    assert.doesNotMatch(attrs()["gen_ai.output.messages"], /NOT_MODEL_OUTPUT/);
    assert.match(
      (await exported())["gen_ai.output.messages"],
      /CURRENT_OUTPUT_SENTINEL/,
    );
  } finally {
    h.dispose();
    await provider.forceFlush();
  }
});
test("released step.delta SSE captures ordered repeated parts without snapshot duplicates", async () => {
  for (const snapshot of [false, true]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai);
    const events = [
      {
        event_type: "interaction.created",
        interaction: {
          id: "i-stream",
          model: "gemini-test",
          status: "in_progress",
        },
      },
      {
        event_type: "step.start",
        index: 0,
        step: { type: "model_output", content: [] },
      },
      ...["same", "same", "different"].map((text) => ({
        event_type: "step.delta",
        index: 0,
        delta: { type: "text", text },
      })),
      { event_type: "step.stop", index: 0 },
      {
        event_type: "interaction.completed",
        interaction: {
          id: "i-stream",
          status: "completed",
          ...(snapshot
            ? {
                steps: [
                  {
                    type: "model_output",
                    content: [{ type: "text", text: "samesamedifferent" }],
                  },
                ],
              }
            : {}),
        },
      },
    ];
    handler = (_, res) => sse(res, events);
    try {
      const received = [];
      for await (const e of await ai.interactions.create({
        model: "gemini-test",
        input: "hello",
        stream: true,
        automatic_continuation: false,
      }))
        received.push(e);
      assert.deepEqual(received, events);
      const output = JSON.parse(attrs()["gen_ai.output.messages"] || "[]");
      assert.equal(
        output
          .flatMap((m) => m.content)
          .map((p) => p.text)
          .join(""),
        "samesamedifferent",
      );
      assert.equal(attrs()["google.genai.status"], "completed");
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});
test("in-band Interactions errors retain caller events but export only generic error metadata", async () => {
  for (const traceConfig of [
    {},
    { hideInputs: true, hideOutputs: true, hideErrorDetails: true },
  ]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai, { traceConfig });
    const events = [
      {
        event_type: "interaction.created",
        interaction: { id: "i-error", status: "in_progress" },
      },
      {
        event_type: "error",
        error: { code: "INTERNAL", message: "PRIVATE_ERROR_SENTINEL" },
      },
    ];
    handler = (_, res) => sse(res, events);
    try {
      const received = [];
      for await (const e of await ai.interactions.create({
        model: "gemini-test",
        input: "hello",
        stream: true,
        automatic_continuation: false,
      }))
        received.push(e);
      assert.deepEqual(received, events);
      assert.equal(spans()[0].status.code, 2);
      assert.equal(attrs()["error.type"], "provider_error");
      assert.equal(attrs()["google.genai.status"], "failed");
      await exported();
      assert.doesNotMatch(JSON.stringify(wire), /PRIVATE_ERROR_SENTINEL/);
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});
const toolResult = [
  { functionResponse: { name: "weather", response: { weather: "sunny" } } },
];
test("automatic-tool usage includes every hop, not only the last response", async () => {
  for (const streaming of [false, true]) {
    async function run(tracing) {
      reset();
      const ai = client();
      const h = tracing && instrumentGoogleGenAI(ai);
      let resolutions = 0,
        executions = 0;
      const tool = {
        async tool() {
          resolutions++;
          return {
            functionDeclarations: [
              { name: "weather", description: "get weather" },
            ],
          };
        },
        async callTool() {
          executions++;
          return toolResult;
        },
      };
      handler = (_, res) => {
        const body =
          requests.length === 1
            ? {
                candidates: [
                  {
                    index: 0,
                    content: {
                      role: "model",
                      parts: [
                        {
                          functionCall: {
                            name: "weather",
                            args: { city: "London" },
                          },
                        },
                      ],
                    },
                    finishReason: "STOP",
                  },
                ],
                usageMetadata: {
                  promptTokenCount: 60,
                  candidatesTokenCount: 40,
                  totalTokenCount: 100,
                  promptTokensDetails: [{ modality: "AUDIO", tokenCount: 6 }],
                  candidatesTokensDetails: [{ modality: "AUDIO", tokenCount: 4 }, { modality: "IMAGE", tokenCount: 1 }],
                },
              }
            : answer("sunny", {
                promptTokenCount: 4, candidatesTokenCount: 2, totalTokenCount: 6,
                promptTokensDetails: [{ modality: "AUDIO", tokenCount: 2 }],
                candidatesTokensDetails: [{ modality: "AUDIO", tokenCount: 1 }, { modality: "IMAGE", tokenCount: 1 }],
              });
        if (streaming)
          sse(res, [
            { usageMetadata: { ...body.usageMetadata, totalTokenCount: 1 } },
            body,
          ]);
        else json(res, body);
      };
      try {
        const params = {
          model: "gemini-test",
          contents: "weather?",
          config: {
            tools: [tool],
            automaticFunctionCalling: { maximumRemoteCalls: 2 },
          },
        };
        const result = streaming
          ? await ai.models.generateContentStream(params)
          : await ai.models.generateContent(params);
        const delivered = [];
        if (streaming) {
          for await (const chunk of result)
            delivered.push(JSON.parse(JSON.stringify(chunk)));
        } else delivered.push(JSON.parse(JSON.stringify(result)));
        return {
          requests: structuredClone(requests),
          delivered,
          resolutions,
          executions,
        };
      } finally {
        if (h) h.dispose();
        await provider.forceFlush();
      }
    }
    const baseline = await run(false);
    const observed = await run(true);
    assert.deepEqual(observed, baseline);
    assert.equal(observed.requests.length, 2);
    assert.equal(observed.executions, 1);
    assert.equal(attrs()["gen_ai.usage.total_tokens"], 106);
    assert.equal(attrs()["gen_ai.usage.input_tokens"], 64);
    assert.equal(attrs()["gen_ai.usage.output_tokens"], 42);
    assert.equal(attrs()["gen_ai.usage.input_tokens.audio"], 8);
    assert.equal(attrs()["gen_ai.usage.output_tokens.audio"], 5);
    assert.equal(attrs()["gen_ai.usage.output_tokens.image"], 2);
    assert.equal(Number((await exported())["gen_ai.usage.total_tokens"]), 106);
  }
});
test("Interactions top-level system_instruction honors all input privacy controls", async () => {
  for (const hidden of [
    undefined,
    "hideInputs",
    "hideInputMessages",
    "hideInputText",
  ]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai, {
      traceConfig: hidden ? { [hidden]: true } : {},
    });
    handler = (_, res) => json(res, { id: "i-system", status: "completed" });
    try {
      await ai.interactions.create({
        model: "gemini-test",
        input: "hello",
        system_instruction: "SYSTEM_SENTINEL",
        automatic_continuation: false,
      });
      assert.equal(requests[0].system_instruction, "SYSTEM_SENTINEL");
      if (hidden) assert.equal(attrs()["gen_ai.input.messages"], undefined);
      else
        assert.deepEqual(JSON.parse(attrs()["gen_ai.input.messages"])[0], {
          role: "system",
          content: [{ type: "text", text: "SYSTEM_SENTINEL" }],
        });
      const data = await exported();
      if (hidden) assert.doesNotMatch(JSON.stringify(data), /SYSTEM_SENTINEL/);
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});
test("Interactions Usage.total_thought_tokens maps to reasoning tokens", async () => {
  reset();
  const ai = client();
  const h = instrumentGoogleGenAI(ai);
  handler = (_, res) =>
    json(res, {
      id: "i-thought",
      status: "completed",
      usage: {
        total_input_tokens: 10,
        total_output_tokens: 3,
        total_thought_tokens: 7,
        total_tokens: 20,
      },
    });
  try {
    const response = await ai.interactions.create({
      model: "gemini-test",
      input: "hello",
      automatic_continuation: false,
    });
    assert.equal(response.usage.total_thought_tokens, 7);
    assert.equal(attrs()["gen_ai.usage.output_tokens.reasoning"], 7);
    assert.equal(
      Number((await exported())["gen_ai.usage.output_tokens.reasoning"]),
      7,
    );
  } finally {
    h.dispose();
    await provider.forceFlush();
  }
});
test("CallableTool declarations are observed after SDK resolution, never resolved twice", async () => {
  for (const traceConfig of [{}, { hideInputText: true }]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai, { traceConfig });
    let resolutions = 0;
    handler = (_, res) => json(res, answer("sunny"));
    const tool = {
      async tool() {
        assert.equal(this, tool);
        resolutions++;
        return {
          functionDeclarations: [
            { name: "weather", description: "TOOL_DESCRIPTION_SENTINEL" },
          ],
        };
      },
      async callTool() {
        assert.fail("No tool was requested");
      },
    };
    try {
      await ai.models.generateContent({
        model: "gemini-test",
        contents: "weather?",
        config: { tools: [tool] },
      });
      assert.equal(resolutions, 1);
      assert.equal(
        requests[0].tools[0].functionDeclarations[0].name,
        "weather",
      );
      if (traceConfig.hideInputText)
        assert.equal(attrs()["gen_ai.tool.definitions"], undefined);
      else
        assert.deepEqual(
          JSON.parse(attrs()["gen_ai.tool.definitions"]),
          requests[0].tools,
        );
      const data = await exported();
      if (traceConfig.hideInputText)
        assert.doesNotMatch(JSON.stringify(data), /TOOL_DESCRIPTION_SENTINEL/);
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});
test("per-string, list and depth limits mark deliberately truncated capture", async () => {
  for (const shape of ["text", "parts", "depth"]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai);
    handler = (_, res) => {
      const body = answer("ok");
      body.candidates[0].content.parts =
        shape === "text"
          ? [{ text: "a".repeat(5000) + "TAIL_SENTINEL" }]
          : shape === "parts"
            ? Array.from({ length: 65 }, () => ({ text: "a" }))
            : [
                {
                  functionCall: {
                    name: "deep",
                    args: {
                      a: { b: { c: { d: { e: { f: { g: "deep" } } } } } },
                    },
                  },
                },
              ];
      json(res, body);
    };
    try {
      await ai.models.generateContent({
        model: "gemini-test",
        contents: "short",
      });
      assert.equal(attrs()["google.genai.capture_truncated"], true, shape);
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});

test("generation continuation counts independent hop snapshots without counting merged public totals", async () => {
  for (const streaming of [false, true]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai);
    handler = (_, res) => {
      const body =
        requests.length === 1
          ? answer("first", {
              promptTokenCount: 60,
              candidatesTokenCount: 40,
              totalTokenCount: 100,
            })
          : answer("second");
      if (requests.length === 1)
        Object.assign(body.candidates[0], {
          finishReason: "CONTINUATION",
          continuationToken: "continue-1",
        });
      if (streaming) sse(res, [body]);
      else json(res, body);
    };
    try {
      const params = {
        model: "gemini-test",
        contents: "continue",
        config: { automaticContinuation: true },
      };
      if (streaming) {
        for await (const chunk of await ai.models.generateContentStream(params))
          assert.ok(chunk);
      } else await ai.models.generateContent(params);
      // Older supported releases do not implement continuation; do not fabricate hops for them.
      assert.ok([1, 2].includes(requests.length));
      assert.equal(
        attrs()["gen_ai.usage.total_tokens"],
        requests.length === 2 ? 106 : 100,
      );
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});
test("partial or inaccessible hop usage is explicit rather than invented call totals", async () => {
  for (const frozen of [false, true]) {
    reset();
    const ai = client();
    if (frozen)
      Object.defineProperty(ai.models, "generateContentInternal", {
        value: ai.models.generateContentInternal,
        writable: false,
        configurable: false,
      });
    const h = instrumentGoogleGenAI(ai);
    handler = (_, res) => {
      const body =
        requests.length === 1
          ? {
              candidates: [
                {
                  content: {
                    role: "model",
                    parts: [{ functionCall: { name: "weather", args: {} } }],
                  },
                },
              ],
            }
          : answer("sunny");
      json(res, body);
    };
    const tool = {
      async tool() {
        return { functionDeclarations: [{ name: "weather" }] };
      },
      async callTool() {
        return toolResult;
      },
    };
    try {
      const result = await ai.models.generateContent({
        model: "gemini-test",
        contents: "weather",
        config: {
          tools: [tool],
          automaticFunctionCalling: { maximumRemoteCalls: 2 },
        },
      });
      assert.equal(result.text, "sunny");
      assert.equal(requests.length, 2);
      assert.equal(
        attrs()["google.genai.usage_capture"],
        frozen ? "unavailable" : "partial",
      );
      assert.equal(
        attrs()["gen_ai.usage.total_tokens"],
        frozen ? undefined : 6,
      );
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
  }
});
test("dispose and re-instrument during automatic calling keeps old and new calls isolated", async () => {
  reset();
  const ai = client();
  const original = ai.models.generateContentInternal;
  const h = instrumentGoogleGenAI(ai);
  let replacement;
  handler = (_, res) =>
    json(
      res,
      requests.length === 1
        ? {
            candidates: [
              {
                content: {
                  role: "model",
                  parts: [{ functionCall: { name: "weather", args: {} } }],
                },
              },
            ],
            usageMetadata: { totalTokenCount: 100 },
          }
        : answer("sunny"),
    );
  const tool = {
    async tool() {
      return { functionDeclarations: [{ name: "weather" }] };
    },
    async callTool() {
      h.dispose();
      replacement = instrumentGoogleGenAI(ai, {
        traceConfig: { hideInputs: true },
      });
      await ai.models.generateContent({
        model: "nested",
        contents: "PRIVATE_NESTED_INPUT",
      });
      return toolResult;
    },
  };
  try {
    await ai.models.generateContent({
      model: "outer",
      contents: "weather?",
      config: {
        tools: [tool],
        automaticFunctionCalling: { maximumRemoteCalls: 2 },
      },
    });
    assert.equal(requests.length, 3);
    assert.equal(spans().length, 2);
    const outer = spans().find(
      (s) => s.attributes["gen_ai.request.model"] === "outer",
    );
    const nested = spans().find(
      (s) => s.attributes["gen_ai.request.model"] === "nested",
    );
    assert.equal(outer.attributes["gen_ai.usage.total_tokens"], 106);
    assert.equal(nested.attributes["gen_ai.usage.total_tokens"], 6);
    assert.equal(nested.attributes["gen_ai.input.messages"], undefined);
    replacement.dispose();
    assert.equal(ai.models.generateContentInternal, original);
  } finally {
    h.dispose();
    replacement?.dispose();
    await provider.forceFlush();
  }
});
test("concurrent calls on one client retain their resolved tools and hop totals", async () => {
  reset();
  const ai = client();
  const h = instrumentGoogleGenAI(ai);
  handler = (_, res) => {
    const body = requests.at(-1),
      name = body.tools[0].functionDeclarations[0].name;
    const last = body.contents.at(-1).parts[0];
    if (last.functionResponse)
      json(res, answer(name, { totalTokenCount: name === "alpha" ? 6 : 8 }));
    else
      json(res, {
        candidates: [
          {
            content: {
              role: "model",
              parts: [{ functionCall: { name, args: {} } }],
            },
          },
        ],
        usageMetadata: { totalTokenCount: name === "alpha" ? 100 : 200 },
      });
  };
  try {
    await Promise.all(
      ["alpha", "beta"].map((name) =>
        ai.models.generateContent({
          model: name,
          contents: name,
          config: {
            tools: [
              {
                async tool() {
                  return { functionDeclarations: [{ name }] };
                },
                async callTool() {
                  return [
                    { functionResponse: { name, response: { result: "ok" } } },
                  ];
                },
              },
            ],
            automaticFunctionCalling: { maximumRemoteCalls: 2 },
          },
        }),
      ),
    );
    assert.equal(spans().length, 2);
    assert.equal(requests.length, 4);
    for (const span of spans()) {
      const name = span.attributes["gen_ai.request.model"];
      assert.equal(
        span.attributes["gen_ai.usage.total_tokens"],
        name === "alpha" ? 106 : 208,
      );
      assert.deepEqual(JSON.parse(span.attributes["gen_ai.tool.definitions"]), [
        { functionDeclarations: [{ name }] },
      ]);
    }
  } finally {
    h.dispose();
    await provider.forceFlush();
  }
});

test("untraced nested calls never contribute usage to an active automatic-tool call", async () => {
  const { context } = req("@opentelemetry/api");
  const { suppressTracing } = req("@opentelemetry/core");
  for (const mode of ["disable", "dispose", "suppress"]) {
    reset();
    const ai = client();
    const original = ai.models.generateContentInternal;
    const h = instrumentGoogleGenAI(ai);
    handler = (_, res) =>
      json(
        res,
        requests.length === 1
          ? {
              candidates: [
                {
                  content: {
                    role: "model",
                    parts: [{ functionCall: { name: "weather", args: {} } }],
                  },
                },
              ],
              usageMetadata: { totalTokenCount: 100 },
            }
          : answer("sunny"),
      );
    const tool = {
      async tool() {
        return { functionDeclarations: [{ name: "weather" }] };
      },
      async callTool() {
        const nested = () =>
          ai.models.generateContent({
            model: "nested",
            contents: "not part of outer usage",
          });
        if (mode === "suppress")
          await context.with(suppressTracing(context.active()), nested);
        else {
          h[mode]();
          await nested();
        }
        return toolResult;
      },
    };
    try {
      await ai.models.generateContent({
        model: "outer",
        contents: "weather?",
        config: {
          tools: [tool],
          automaticFunctionCalling: { maximumRemoteCalls: 2 },
        },
      });
      assert.equal(requests.length, 3);
      assert.equal(attrs()["gen_ai.usage.total_tokens"], 106, mode);
    } finally {
      h.dispose();
      await provider.forceFlush();
    }
    assert.equal(ai.models.generateContentInternal, original);
  }
});

// Exercise the real SDK response classes, including cumulative SSE snapshots,
// at both supported checkpoints. Content hiding must not erase cost metadata.
const modalityUsage = {
  promptTokenCount: 11, candidatesTokenCount: 7, totalTokenCount: 23,
  thoughtsTokenCount: 5, cachedContentTokenCount: 2,
  promptTokensDetails: [{ modality: "TEXT", tokenCount: 5 }, { modality: "AUDIO", tokenCount: 6 }],
  candidatesTokensDetails: [{ modality: "TEXT", tokenCount: 2 }, { modality: "AUDIO", tokenCount: 4 }, { modality: "IMAGE", tokenCount: 1 }],
  cacheTokensDetails: [{ modality: "AUDIO", tokenCount: 2 }],
};
for (const streaming of [false, true]) {
  test(`generation modality cost inputs survive ${streaming ? "cumulative SSE" : "generation"} and hiding`, async () => {
    for (const hidden of [false, true]) {
      reset();
      const ai = client();
      const h = instrumentGoogleGenAI(ai, { traceConfig: { hideInputs: hidden, hideOutputs: hidden } });
      const body = answer("PRIVATE_OUTPUT", modalityUsage);
      handler = (_, res) => streaming ? sse(res, [
        { usageMetadata: { promptTokenCount: 1, promptTokensDetails: [{ modality: "AUDIO", tokenCount: 1 }] } },
        body, { usageMetadata: modalityUsage },
      ]) : json(res, body);
      try {
        let response = await ai.models[streaming ? "generateContentStream" : "generateContent"]({ model: "gemini-test", contents: "PRIVATE_INPUT" });
        if (streaming) for await (const chunk of response) response = chunk;
        assert.deepEqual(response.usageMetadata.promptTokensDetails, modalityUsage.promptTokensDetails);
        assert.equal(attrs()["gen_ai.usage.input_tokens.audio"], 6);
        assert.equal(attrs()["gen_ai.usage.output_tokens.audio"], 4);
        assert.equal(attrs()["gen_ai.usage.output_tokens.image"], 1);
        assert.equal(attrs()["gen_ai.usage.cache_read_tokens.audio"], 2);
        assert.equal(attrs()["gen_ai.usage.input_tokens"], 11);
        assert.equal(attrs()["gen_ai.usage.total_tokens"], 23);
        const data = await exported();
        assert.equal(Number(data["gen_ai.usage.input_tokens.audio"]), 6);
        assert.equal(Number(data["gen_ai.usage.output_tokens.image"]), 1);
        if (hidden) assert.doesNotMatch(JSON.stringify(data), /PRIVATE_INPUT|PRIVATE_OUTPUT/);
      } finally { h.dispose(); await provider.forceFlush(); }
    }
  });
  test(`Interactions modality cost inputs survive ${streaming ? "cumulative SSE" : "generation"}`, async () => {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai, { traceConfig: { hideInputs: true, hideOutputs: true } });
    const usage = {
      total_input_tokens: 11, total_output_tokens: 7, total_tokens: 23,
      input_tokens_by_modality: [{ modality: "audio", tokens: 2 }, { modality: "AUDIO", tokens: 4 }, { modality: "text", tokens: 5 }],
      output_tokens_by_modality: [{ modality: "audio", tokens: 4 }, { modality: "image", tokens: 1 }],
    };
    const body = { id: "i-modality", status: "completed", usage };
    const { eventType } = require("./interaction-fixtures.cjs");
    const events = [
      { event_type: eventType("interaction.start"), interaction: { id: "i-modality", status: "in_progress", usage } },
      { event_type: eventType("interaction.complete"), interaction: body },
    ];
    handler = (_, res) => streaming ? sse(res, events) : json(res, body);
    try {
      const response = await ai.interactions.create({ model: "gemini-test", input: "PRIVATE_INPUT", stream: streaming, automatic_continuation: false });
      if (streaming) { const received = []; for await (const event of response) received.push(event); assert.deepEqual(received, events); }
      else assert.deepEqual(response.usage, usage);
      assert.equal(attrs()["gen_ai.usage.input_tokens.audio"], 6);
      assert.equal(attrs()["gen_ai.usage.output_tokens.audio"], 4);
      assert.equal(attrs()["gen_ai.usage.output_tokens.image"], 1);
      assert.equal(attrs()["gen_ai.usage.total_tokens"], 23);
      assert.equal(Number((await exported())["gen_ai.usage.output_tokens.audio"]), 4);
    } finally { h.dispose(); await provider.forceFlush(); }
  });
}

for (const spelling of ['promptTokenDetails', 'promptTokensDetails']) {
  for (const hidden of [false, true]) {
    test(`embedding captures ${spelling} before SDK conversion; hidden=${hidden}`, async () => {
      reset();
      const ai = client();
      const h = instrumentGoogleGenAI(ai, { traceConfig: { hideInputs: hidden, hideOutputs: hidden, hideEmbeddingVectors: true } });
      handler = (_, res) => json(res, {
        embeddings: [{ values: [0.123456789] }],
        usageMetadata: { promptTokenCount: 10,
          [spelling]: [{ modality: 'AUDIO', tokenCount: 8 }, { modality: 'TEXT', tokenCount: 2 }],
          privateField: 'USAGE_PRIVATE_SENTINEL' },
      });
      try {
        const result = await ai.models.embedContent({ model: 'gemini-embedding-2', contents: [
          { parts: [{ text: 'INPUT_SENTINEL' }, { inlineData: { mimeType: 'audio/wav', data: 'U1lOVEhFVElD' } }] },
        ] });
        assert.deepEqual(result.embeddings[0].values, [0.123456789]);
        assert.equal(requests.length, 1);
        assert.equal(attrs()['gen_ai.usage.input_tokens'], 10);
        assert.equal(attrs()['gen_ai.usage.total_tokens'], 10);
        assert.equal(attrs()['gen_ai.usage.input_tokens.audio'], 8);
        assert.equal(attrs()['gen_ai.usage.input_tokens.text'], 2);
        assert.doesNotMatch(JSON.stringify(attrs()), /USAGE_PRIVATE_SENTINEL|0\.123456789|U1lOVEhFVElD|synthetic-google-key/);
        const wireAttrs = await exported();
        assert.equal(Number(wireAttrs['gen_ai.usage.input_tokens']), 10);
        assert.equal(Number(wireAttrs['gen_ai.usage.total_tokens']), 10);
        assert.equal(Number(wireAttrs['gen_ai.usage.input_tokens.audio']), 8);
        assert.equal(Number(wireAttrs['gen_ai.usage.input_tokens.text']), 2);
        if (hidden) assert.doesNotMatch(JSON.stringify(wireAttrs), /INPUT_SENTINEL/);
      } finally { h.dispose(); await provider.forceFlush(); }
    });
  }
}

for (const hidden of [false, true]) {
  test(`Vertex embedding statistics supply complete input and total usage; hidden=${hidden}`, async () => {
    reset();
    const ai = new GoogleGenAI({
      vertexai: true,
      apiKey: 'synthetic-vertex-key',
      httpOptions: { baseUrl, retryOptions: { attempts: 1 } },
    });
    const paths = [];
    handler = (request, res) => {
      paths.push(request.url);
      json(res, {
        predictions: [7, 11].map(token_count => ({
          embeddings: { statistics: { truncated: false, token_count }, values: [0.123456789] },
        })),
        metadata: { billableCharacterCount: 50 },
      });
    };
    const params = { model: 'text-embedding-005', contents: ['INPUT_SENTINEL_ONE', 'INPUT_SENTINEL_TWO'] };
    const baseline = await ai.models.embedContent(params);
    const h = instrumentGoogleGenAI(ai, { traceConfig: { hideInputs: hidden, hideOutputs: hidden } });
    try {
      const result = await ai.models.embedContent(params);
      assert.deepEqual(result, baseline);
      assert.deepEqual(result.embeddings.map(e => e.statistics.tokenCount), [7, 11]);
      assert.equal(requests.length, 2);
      assert.deepEqual(requests[0], requests[1]);
      assert.equal(paths[0], paths[1]);
      assert.match(paths[1], /:predict/);
      assert.equal(attrs()['embedding.count'], 2);
      assert.equal(attrs()['gen_ai.usage.input_tokens'], 18);
      assert.equal(attrs()['gen_ai.usage.total_tokens'], 18);
      assert.equal(attrs()['gen_ai.usage.output_tokens'], undefined);
      const wireAttrs = await exported();
      assert.equal(Number(wireAttrs['gen_ai.usage.input_tokens']), 18);
      assert.equal(Number(wireAttrs['gen_ai.usage.total_tokens']), 18);
      assert.doesNotMatch(JSON.stringify(wireAttrs), /0\.123456789|synthetic-vertex-key/);
      if (hidden) assert.doesNotMatch(JSON.stringify(wireAttrs), /INPUT_SENTINEL/);
    } finally { h.dispose(); await provider.forceFlush(); }
  });
}

test('embedding statistics fallback prefers aggregates and never invents partial totals', async () => {
  const cases = [
    { label: 'complete', counts: [7, 11], input: 18, total: 18 },
    { label: 'zero', counts: [0, 0], input: 0, total: 0 },
    { label: 'empty', counts: [] },
    ...[undefined, null, '11', -1, NaN, Infinity].map(count => ({
      label: `invalid ${String(count)}`, counts: [7, count],
    })),
    { label: 'overflow', counts: [Number.MAX_VALUE, Number.MAX_VALUE] },
    { label: 'aggregate input', counts: [7, 11], usage: { promptTokenCount: 30 }, input: 30, total: 30 },
    { label: 'aggregate zero', counts: [7, 11], usage: { promptTokenCount: 0 }, input: 0, total: 0 },
    { label: 'explicit total', counts: [7, 11], usage: { totalTokenCount: 40 }, input: 18, total: 40 },
    { label: 'aggregate both', counts: [7, 11], usage: { promptTokenCount: 30, totalTokenCount: 40 }, input: 30, total: 40 },
    { label: 'partial stats with aggregate', counts: [7, undefined], usage: { promptTokenCount: 30 }, input: 30, total: 30 },
    { label: 'partial stats with only total', counts: [7, undefined], usage: { totalTokenCount: 40 }, total: 40 },
  ];
  // Older SDKs discard aggregate metadata; newer SDKs may expose it again on the
  // public result. Neither path may add that snapshot to per-embedding counts.
  for (const retainUsage of [false, true]) {
    for (const c of cases) {
      reset();
      const raw = { usageMetadata: c.usage };
      let reads = 0;
      const response = { async json() { reads++; return raw; } };
      const apiClient = { async request() { return response; } };
      const result = {
        embeddings: c.counts.map(tokenCount => ({ values: [0.123456789], statistics: { tokenCount } })),
        ...(retainUsage ? { usageMetadata: c.usage } : {}),
      };
      const ai = { models: { apiClient, async embedContent() {
        await (await apiClient.request({ path: 'models/test:predict' })).json();
        return result;
      } } };
      const h = instrumentGoogleGenAI(ai);
      try {
        assert.equal(await ai.models.embedContent({ model: 'test' }), result);
        assert.equal(reads, 1);
        assert.equal(attrs()['gen_ai.usage.input_tokens'], c.input, c.label);
        assert.equal(attrs()['gen_ai.usage.total_tokens'], c.total, c.label);
        assert.equal(attrs()['gen_ai.usage.output_tokens'], undefined, c.label);
        assert.doesNotMatch(JSON.stringify(attrs()), /0\.123456789/);
      } finally { h.dispose(); await provider.forceFlush(); }
    }
  }
});

test('embedding modality aliases are alternatives with numeric allowlisted counts only', async () => {
  for (const both of [false, true]) {
    reset();
    const ai = client();
    const h = instrumentGoogleGenAI(ai, { traceConfig: { hideInputs: true, hideOutputs: true } });
    const details = [
      { modality: 'AUDIO', tokenCount: 8 }, { modality: 'TEXT', tokenCount: 2 },
      { modality: 'IMAGE', tokenCount: 0 }, { modality: 'PRIVATE_MODALITY', tokenCount: 100 },
      { modality: 'AUDIO', tokenCount: -1 }, { modality: 'AUDIO', tokenCount: '100' },
      { modality: 'VIDEO', tokenCount: null },
    ];
    handler = (_, res) => json(res, {
      embeddings: [{ values: [0.123456789] }],
      usageMetadata: { promptTokenCount: 10, promptTokenDetails: details,
        ...(both ? { promptTokensDetails: details } : {}) },
    });
    try {
      await ai.models.embedContent({ model: 'gemini-embedding-2', contents: 'INPUT_SENTINEL' });
      assert.equal(attrs()['gen_ai.usage.input_tokens.audio'], 8);
      assert.equal(attrs()['gen_ai.usage.input_tokens.text'], 2);
      assert.equal(attrs()['gen_ai.usage.input_tokens.image'], 0);
      assert.equal(attrs()['gen_ai.usage.input_tokens.video'], undefined);
      assert.doesNotMatch(JSON.stringify(attrs()), /PRIVATE_MODALITY|INPUT_SENTINEL|0\.123456789/);
    } finally { h.dispose(); await provider.forceFlush(); }
  }
});

test('embedding parse scope isolates concurrent clients and survives disposal until calls drain', { timeout: 15000 }, async () => {
  reset();
  const { context } = req('@opentelemetry/api');
  const { suppressTracing } = req('@opentelemetry/core');
  const a = client(), b = client(), untraced = client();
  const originalA = a.models.apiClient.request;
  const originalB = b.models.apiClient.request;
  const originalUntraced = untraced.models.apiClient.request;
  const h = instrumentGoogleGenAI(a), other = instrumentGoogleGenAI(b);
  const waiting = [];
  let allArrived;
  const arrived = new Promise(resolve => { allArrived = resolve; });
  handler = (request, res) => {
    const tokens = request.url.includes('alpha') ? 11 : request.url.includes('beta') ? 22 : 99;
    waiting.push(() => json(res, { embeddings: [{ values: [0.9] }],
      usageMetadata: { promptTokenCount: tokens, totalTokenCount: tokens } }));
    if (waiting.length === 4) allArrived();
  };
  const embed = (ai, model) => ai.models.embedContent({ model, contents: 'private text' });
  try {
    const pending = [embed(a, 'alpha'), embed(b, 'beta'), embed(untraced, 'untraced'),
      context.with(suppressTracing(context.active()), () => embed(a, 'suppressed'))];
    await arrived;
    h.dispose();
    assert.notEqual(a.models.apiClient.request, originalA, 'in-flight parse must remain observed');
    waiting.reverse().forEach(respond => respond());
    await Promise.all(pending);
    assert.equal(requests.length, 4);
    assert.equal(spans().length, 2);
    const byModel = Object.fromEntries(spans().map(s => [s.attributes['gen_ai.request.model'], s.attributes['gen_ai.usage.input_tokens']]));
    assert.deepEqual(byModel, { alpha: 11, beta: 22 });
    assert.equal(a.models.apiClient.request, originalA);
    assert.equal(untraced.models.apiClient.request, originalUntraced);
    handler = (_, res) => json(res, { embeddings: [{ values: [0.9] }], usageMetadata: { promptTokenCount: 55 } });
    other.disable();
    await embed(b, 'disabled');
    await embed(a, 'disposed');
    assert.equal(spans().length, 2);
    other.enable();
    await embed(b, 'enabled');
    assert.equal(spans().length, 3);
    assert.equal(spans()[2].attributes['gen_ai.usage.input_tokens'], 55);
  } finally {
    waiting.forEach(respond => { try { respond(); } catch {} });
    h.dispose(); other.dispose();
    assert.equal(b.models.apiClient.request, originalB);
  }
});

test('embedding parse observation preserves identity and errors, reads once, and allowlists numbers only', async () => {
  reset();
  let reads = 0, clones = 0, calls = 0, result, parsedValue, failure;
  const vectors = { get embeddings() { throw new Error('must never inspect raw vectors'); } };
  const raw = Object.assign(vectors, { usageMetadata: {
    promptTokenCount: 7, totalTokenCount: 9, candidatesTokenCount: 'PRIVATE_STRING',
    cachedContentTokenCount: Infinity, thoughtsTokenCount: NaN,
    promptTokensDetails: [{ modality: 'AUDIO', tokenCount: 5 }, { modality: 'PRIVATE_MODALITY', tokenCount: 100 }],
    credential: 'PRIVATE_USAGE',
  } });
  const response = {
    json() { reads++; return failure ? Promise.reject(failure) : Promise.resolve(raw); },
    clone() { clones++; throw new Error('must never clone'); },
  };
  const originalParse = response.json;
  const apiClient = { async request() { calls++; return response; } };
  const originalRequest = apiClient.request;
  const ai = { models: {
    apiClient,
    async embedContent() {
      const received = await apiClient.request({ path: 'models/test:batchEmbedContents' });
      assert.equal(received, response);
      parsedValue = await received.json();
      assert.equal(parsedValue, raw);
      return result;
    },
  } };
  result = { embeddings: [{ values: [1, 2] }] };
  const h = instrumentGoogleGenAI(ai);
  try {
    assert.equal(await ai.models.embedContent({ model: 'test', contents: 'hi' }), result);
    assert.equal(response.json, originalParse);
    assert.equal(reads, 1); assert.equal(clones, 0); assert.equal(calls, 1);
    assert.equal(attrs()['gen_ai.usage.input_tokens'], 7);
    assert.equal(attrs()['gen_ai.usage.total_tokens'], 9);
    assert.equal(attrs()['gen_ai.usage.input_tokens.audio'], 5);
    assert.doesNotMatch(JSON.stringify(attrs()), /PRIVATE_STRING|PRIVATE_MODALITY|PRIVATE_USAGE/);
    assert.equal(attrs()['gen_ai.usage.output_tokens'], undefined);
    memory.reset();
    failure = new Error('PROVIDER_PRIVATE_ERROR');
    await assert.rejects(ai.models.embedContent({ model: 'test' }), e => e === failure);
    assert.equal(reads, 2); assert.equal(clones, 0); assert.equal(calls, 2);
    assert.equal(response.json, originalParse);
    assert.equal(spans()[0].status.code, 2);
    assert.doesNotMatch(JSON.stringify(attrs()), /PROVIDER_PRIVATE_ERROR/);
  } finally {
    h.dispose();
    assert.equal(apiClient.request, originalRequest);
  }
});

test('embedding observation fails open on frozen boundaries and capture getters', async () => {
  for (const frozen of ['request', 'json', 'usage']) {
    reset();
    const raw = frozen === 'usage' ? { get usageMetadata() { throw new Error('PRIVATE_GETTER'); } }
      : { usageMetadata: { promptTokenCount: 4 } };
    const response = { json: async () => raw };
    const apiClient = { request: async () => response };
    if (frozen === 'request') Object.freeze(apiClient);
    if (frozen === 'json') Object.freeze(response);
    const result = { embeddings: [{ values: [0.8] }] };
    const ai = { models: { apiClient, async embedContent() {
      const httpResponse = await apiClient.request({ path: 'models/test:embedContent' });
      assert.equal(await httpResponse.json(), raw);
      return result;
    } } };
    const h = instrumentGoogleGenAI(ai);
    try {
      assert.equal(await ai.models.embedContent({ model: 'test' }), result);
      assert.equal(spans().length, 1);
      assert.notEqual(spans()[0].status.code, 2);
      assert.doesNotMatch(JSON.stringify(attrs()), /PRIVATE_GETTER/);
    } finally { h.dispose(); }
  }
});

test('embedding response hooks drain correctly when a client is re-instrumented in flight', { timeout: 15000 }, async () => {
  reset();
  const ai = client();
  const originalRequest = ai.models.apiClient.request;
  const originalEmbed = ai.models.embedContent;
  const h = instrumentGoogleGenAI(ai);
  const waiting = [];
  let arrived;
  const firstArrival = new Promise(resolve => { arrived = resolve; });
  handler = (request, res) => {
    const count = request.url.includes('old') ? 13 : 27;
    waiting.push(() => json(res, { embeddings: [{ values: [0.6] }],
      usageMetadata: { promptTokenCount: count, totalTokenCount: count } }));
    arrived();
  };
  let replacement;
  try {
    const old = ai.models.embedContent({ model: 'old', contents: 'INPUT_SENTINEL' });
    await firstArrival;
    h.dispose();
    replacement = instrumentGoogleGenAI(ai, { traceConfig: { hideInputs: true } });
    const secondArrival = new Promise(resolve => { arrived = resolve; });
    const next = ai.models.embedContent({ model: 'new', contents: 'INPUT_SENTINEL' });
    await secondArrival;
    waiting[0](); await old;
    assert.notEqual(ai.models.apiClient.request, originalRequest, 'new handle still owns request hook');
    waiting[1](); await next;
    assert.equal(spans().length, 2);
    assert.deepEqual(spans().map(s => s.attributes['gen_ai.usage.input_tokens']), [13, 27]);
    assert.match(JSON.stringify(spans()[0].attributes), /INPUT_SENTINEL/);
    assert.doesNotMatch(JSON.stringify(spans()[1].attributes), /INPUT_SENTINEL/);
    replacement.dispose();
    assert.equal(ai.models.apiClient.request, originalRequest);
    assert.equal(ai.models.embedContent, originalEmbed);
  } finally { h.dispose(); replacement?.dispose(); }
});

test('embedding hook ignores unrelated paths and tolerates capture-only path getters', async () => {
  for (const path of ['models/test:generateContent', 'models/test:predictLongRunning', undefined]) {
    reset();
    const raw = { usageMetadata: { promptTokenCount: 999 } };
    let parsed = 0;
    const response = { async json() { parsed++; return raw; } };
    const originalParse = response.json;
    const apiClient = { async request() { return response; } };
    const params = path ? { path } : { get path() { throw new Error('PRIVATE_PATH_GETTER'); } };
    const result = { embeddings: [{ values: [1] }] };
    const ai = { models: { apiClient, async embedContent() {
      const received = await apiClient.request(params);
      assert.equal(received.json, originalParse);
      assert.equal(await received.json(), raw);
      return result;
    } } };
    const h = instrumentGoogleGenAI(ai);
    try {
      assert.equal(await ai.models.embedContent({ model: 'test' }), result);
      assert.equal(parsed, 1);
      assert.equal(attrs()['gen_ai.usage.input_tokens'], undefined);
      assert.doesNotMatch(JSON.stringify(attrs()), /PRIVATE_PATH_GETTER/);
    } finally { h.dispose(); }
  }
});
