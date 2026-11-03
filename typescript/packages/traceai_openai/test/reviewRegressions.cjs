const { test, after, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const path = require("node:path");
const req = createRequire(
  path.resolve(process.env.OPENAI_CONSUMER_DIR, "package.json"),
);
const OpenAI = req("openai");
const { OpenAIInstrumentation } = req("@traceai/openai");
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req(
  "@opentelemetry/sdk-trace-base",
);
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
let inst;
function instrument(config = {}) {
  inst = new OpenAIInstrumentation({
    instrumentationConfig: { enabled: false },
    traceConfig: config,
  });
  inst.setTracerProvider(provider);
  inst.manuallyInstrument(OpenAI);
}
afterEach(async () => {
  inst?.disable();
  inst = undefined;
  await provider.forceFlush();
  exporter.reset();
});
after(() => provider.shutdown());
function span() {
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  return spans[0];
}
const response = {
  id: "resp-synthetic",
  object: "response",
  status: "completed",
  model: "resolved-model",
  output: [],
  usage: { input_tokens: 2, output_tokens: 3, total_tokens: 5 },
};
const chat = {
  id: "chat-synthetic",
  object: "chat.completion",
  model: "resolved-model",
  choices: [
    {
      index: 0,
      message: { role: "assistant", content: "synthetic" },
      finish_reason: "stop",
    },
  ],
};
function client(body, sse = false) {
  return new OpenAI({
    apiKey: "synthetic-not-a-key",
    maxRetries: 0,
    fetch: async () =>
      new Response(
        sse
          ? body.map((c) => "data: " + JSON.stringify(c) + "\n\n").join("") +
            "data: [DONE]\n\n"
          : JSON.stringify(body),
        {
          headers: {
            "content-type": sse ? "text/event-stream" : "application/json",
          },
        },
      ),
  });
}
function assertNoMarker(marker) {
  const attrs = span().attributes;
  const leaked = Object.keys(attrs).filter((k) =>
    JSON.stringify(attrs[k]).includes(marker),
  );
  assert.deepEqual(leaked, [], `private marker found in ${leaked.join(", ")}`);
}
for (const operation of ["chat", "completions"])
  test(`${operation}: streaming response must not overwrite requested model`, async () => {
    instrument();
    const c = client(
      [
        {
          id: "synthetic",
          object:
            operation === "chat" ? "chat.completion.chunk" : "text_completion",
          model: "resolved-model",
          choices: [
            {
              index: 0,
              ...(operation === "chat"
                ? { delta: { content: "synthetic" } }
                : { text: "synthetic" }),
              finish_reason: "stop",
            },
          ],
        },
      ],
      true,
    );
    const s = await (operation === "chat"
      ? c.chat.completions.create({
          model: "requested-alias",
          messages: [],
          stream: true,
        })
      : c.completions.create({
          model: "requested-alias",
          prompt: "synthetic",
          stream: true,
        }));
    for await (const chunk of s) {
    }
    assert.equal(span().attributes["gen_ai.response.model"], "resolved-model");
    assert.equal(span().attributes["gen_ai.request.model"], "requested-alias");
  });
if (OpenAI.Responses) {
  for (const flag of ["hideInputs", "hideInputText", "hideInputImages"])
    test(`Responses stored prompt variables respect ${flag}`, async () => {
      const marker = "SYNTHETIC-PRIVATE-PROMPT";
      const variable =
        flag === "hideInputImages"
          ? {
              type: "input_image",
              image_url: "https://example.invalid/" + marker,
              detail: "auto",
            }
          : marker;
      const c = client(response);
      const body = {
        model: "synthetic",
        prompt: { id: "pmpt_synthetic", variables: { value: variable } },
      };
      await c.responses.create(body);
      instrument({ [flag]: true });
      await c.responses.create(body);
      assertNoMarker(marker);
    });
  test("Responses MCP credentials are never exported even when inputs are hidden", async () => {
    const marker = "SYNTHETIC-MCP-CREDENTIAL";
    const body = {
      model: "synthetic",
      input: "synthetic",
      tools: [
        {
          type: "mcp",
          server_label: "synthetic",
          server_url: "https://example.invalid/mcp",
          authorization: marker,
          headers: { Authorization: "Bearer " + marker },
        },
      ],
    };
    const c = client(response);
    await c.responses.create(body);
    instrument({ hideInputs: true, hideOutputs: true });
    await c.responses.create(body);
    assertNoMarker(marker);
  });
  test("streamed custom tool call preserves its name and complete input", async () => {
    const c = client(
      [
        {
          id: "chat-synthetic",
          object: "chat.completion.chunk",
          model: "synthetic",
          choices: [
            {
              index: 0,
              delta: {
                tool_calls: [
                  {
                    index: 0,
                    id: "call-synthetic",
                    type: "custom",
                    custom: { name: "code_exec", input: "print(" },
                  },
                ],
              },
              finish_reason: null,
            },
          ],
        },
        {
          id: "chat-synthetic",
          object: "chat.completion.chunk",
          model: "synthetic",
          choices: [
            {
              index: 0,
              delta: { tool_calls: [{ index: 0, custom: { input: "1)" } }] },
              finish_reason: "tool_calls",
            },
          ],
        },
      ],
      true,
    );
    const body = {
      model: "synthetic",
      messages: [],
      stream: true,
      tools: [{ type: "custom", custom: { name: "code_exec" } }],
    };
    const baseline = [];
    for await (const x of await c.chat.completions.create(body))
      baseline.push(x);
    instrument();
    const actual = [];
    for await (const x of await c.chat.completions.create(body)) actual.push(x);
    assert.deepEqual(actual, baseline);
    const tools = JSON.parse(span().attributes["gen_ai.output.messages"])[0]
      .tool_calls;
    assert.deepEqual(tools, [
      {
        id: "call-synthetic",
        type: "custom",
        custom: { name: "code_exec", input: "print(1)" },
      },
    ]);
  });
  for (const direction of ["input", "output"])
    test(`custom tool text is hidden in structured ${direction} messages`, async () => {
      const marker = "SYNTHETIC-PRIVATE-CUSTOM";
      const tool = {
        id: "call-synthetic",
        type: "custom",
        custom: { name: "code_exec", input: marker },
      };
      const c = client(
        direction === "output"
          ? {
              ...chat,
              choices: [
                {
                  index: 0,
                  message: {
                    role: "assistant",
                    content: null,
                    tool_calls: [tool],
                  },
                  finish_reason: "tool_calls",
                },
              ],
            }
          : chat,
      );
      instrument({
        [direction === "input" ? "hideInputText" : "hideOutputText"]: true,
      });
      await c.chat.completions.create({
        model: "synthetic",
        messages:
          direction === "input"
            ? [
                { role: "assistant", content: null, tool_calls: [tool] },
                { role: "tool", tool_call_id: tool.id, content: "synthetic" },
              ]
            : [],
      });
      assertNoMarker(marker);
    });
}
test("parallel streamed function deltas remain separate by tool index", async () => {
  const c = client(
    [
      {
        id: "chat-synthetic",
        object: "chat.completion.chunk",
        model: "synthetic",
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                {
                  index: 0,
                  id: "call-a",
                  type: "function",
                  function: { name: "first", arguments: '{"a":' },
                },
              ],
            },
            finish_reason: null,
          },
        ],
      },
      {
        id: "chat-synthetic",
        object: "chat.completion.chunk",
        model: "synthetic",
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                {
                  index: 1,
                  id: "call-b",
                  type: "function",
                  function: { name: "second", arguments: '{"b":' },
                },
              ],
            },
            finish_reason: null,
          },
        ],
      },
      {
        id: "chat-synthetic",
        object: "chat.completion.chunk",
        model: "synthetic",
        choices: [
          {
            index: 0,
            delta: {
              tool_calls: [
                { index: 0, function: { arguments: "1}" } },
                { index: 1, function: { arguments: "2}" } },
              ],
            },
            finish_reason: "tool_calls",
          },
        ],
      },
    ],
    true,
  );
  instrument();
  for await (const x of await c.chat.completions.create({
    model: "synthetic",
    messages: [],
    stream: true,
    tools: [
      {
        type: "function",
        function: {
          name: "first",
          parameters: { type: "object", properties: { a: { type: "number" } } },
        },
      },
      {
        type: "function",
        function: {
          name: "second",
          parameters: { type: "object", properties: { b: { type: "number" } } },
        },
      },
    ],
  })) {
  }
  const tools = JSON.parse(span().attributes["gen_ai.output.messages"])[0]
    .tool_calls;
  assert.equal(tools.length, 2);
  assert.equal(tools[0].id, "call-a");
  assert.deepEqual(tools[0].function, { name: "first", arguments: '{"a":1}' });
  assert.equal(tools[1].id, "call-b");
  assert.deepEqual(tools[1].function, { name: "second", arguments: '{"b":2}' });
});

if (OpenAI.Responses) {
  for (const config of [{}, { hideInputs: true, hideOutputs: true }]) {
    test(`MCP telemetry is credential-free without changing transport: ${JSON.stringify(config)}`, async () => {
      const marker = "SYNTHETIC-MCP-SECRET";
      const tool = {
        type: "mcp",
        server_label: "synthetic",
        server_url: "https://example.invalid/mcp",
        authorization: marker,
        headers: { Authorization: marker, "X-Custom-Auth": marker },
      };
      const body = { model: "synthetic", input: "public", tools: [tool] };
      const before = structuredClone(body);
      let sent;
      const c = new OpenAI({
        apiKey: "synthetic-not-a-key",
        maxRetries: 0,
        fetch: async (_url, init) => {
          sent = JSON.parse(init.body);
          // The server echoes request configuration. Only output fields belong in
          // telemetry output; tool credentials must not leak through an echo.
          return new Response(JSON.stringify({ ...response, tools: [tool] }), {
            headers: { "content-type": "application/json" },
          });
        },
      });
      instrument(config);
      const actual = await c.responses.create(body);
      assert.deepEqual(sent, before);
      assert.deepEqual(body, before);
      assert.deepEqual(actual.tools, [tool]);
      assertNoMarker(marker);
      const attrs = span().attributes;
      // Export only the origin: even a path can contain bearer credentials.
      const expected = [
        {
          type: "mcp",
          server_label: "synthetic",
          server_url: "https://example.invalid",
        },
      ];
      assert.deepEqual(JSON.parse(attrs["gen_ai.tool.definitions"]), expected);
      assert.deepEqual(
        JSON.parse(attrs["gen_ai.request.parameters"]).tools,
        expected,
      );
    });
  }
  for (const flag of [
    null,
    "hideInputs",
    "hideInputText",
    "hideInputImages",
    "hideInputMessages",
  ]) {
    test(`stored prompt values have a maskable input attribute: ${flag ?? "capture enabled"}`, async () => {
      const marker = "SYNTHETIC-STORED-PROMPT";
      const body = {
        model: "synthetic",
        prompt: {
          id: "pmpt_synthetic",
          version: "2",
          variables: {
            text: marker,
            image: {
              type: "input_image",
              image_url: "https://example.invalid/" + marker,
              detail: "auto",
            },
          },
        },
      };
      const before = structuredClone(body);
      let sent;
      const c = new OpenAI({
        apiKey: "synthetic-not-a-key",
        maxRetries: 0,
        fetch: async (_url, init) => {
          sent = JSON.parse(init.body);
          return new Response(
            JSON.stringify({ ...response, prompt: body.prompt }),
            { headers: { "content-type": "application/json" } },
          );
        },
      });
      instrument(flag ? { [flag]: true } : {});
      await c.responses.create(body);
      assert.deepEqual(sent, before);
      assert.deepEqual(body, before);
      assert.deepEqual(
        JSON.parse(span().attributes["gen_ai.request.parameters"]).prompt,
        { id: "pmpt_synthetic", version: "2" },
      );
      if (flag) assertNoMarker(marker);
      else assert.ok(JSON.stringify(span().attributes).includes(marker));
    });
  }
  test("streamed custom inputs are also covered by granular output hiding", async () => {
    const marker = "SYNTHETIC-STREAM-CUSTOM";
    const c = client(
      [
        {
          id: "chat-synthetic",
          object: "chat.completion.chunk",
          model: "synthetic",
          choices: [
            {
              index: 0,
              delta: {
                tool_calls: [
                  {
                    index: 0,
                    id: "call-custom",
                    type: "custom",
                    custom: { name: "code_exec", input: marker },
                  },
                ],
              },
              finish_reason: "tool_calls",
            },
          ],
        },
      ],
      true,
    );
    instrument({ hideOutputText: true });
    const chunks = [];
    for await (const chunk of await c.chat.completions.create({
      model: "synthetic",
      messages: [],
      stream: true,
    }))
      chunks.push(chunk);
    assert.equal(chunks[0].choices[0].delta.tool_calls[0].custom.input, marker);
    assertNoMarker(marker);
  });
}

test("out-of-order and sparse stream indices preserve tool identity", async () => {
  const deltas = [
    [
      {
        index: 2,
        id: "call-c",
        type: "function",
        function: { name: "thi", arguments: '{"c":' },
      },
    ],
    [
      {
        index: 0,
        id: "call-a",
        type: "function",
        function: { name: "first", arguments: '{"a":' },
      },
    ],
    [
      {
        index: 2,
        id: "call-c",
        type: "function",
        function: { name: "rd", arguments: "3}" },
      },
      { index: 0, function: { arguments: "1}" } },
    ],
  ];
  const chunks = deltas.map((tool_calls) => ({
    id: "chat-synthetic",
    object: "chat.completion.chunk",
    model: "synthetic",
    choices: [{ index: 0, delta: { tool_calls }, finish_reason: null }],
  }));
  const c = client(chunks, true);
  instrument();
  const actual = [];
  for await (const chunk of await c.chat.completions.create({
    model: "synthetic",
    messages: [],
    stream: true,
  }))
    actual.push(chunk);
  assert.deepEqual(actual, chunks);
  assert.deepEqual(
    JSON.parse(span().attributes["gen_ai.output.messages"])[0].tool_calls,
    [
      {
        id: "call-a",
        type: "function",
        function: { name: "first", arguments: '{"a":1}' },
      },
      {
        id: "call-c",
        type: "function",
        function: { name: "third", arguments: '{"c":3}' },
      },
    ],
  );
});
