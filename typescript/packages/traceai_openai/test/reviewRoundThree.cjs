const { test, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const path = require('node:path');
const req = createRequire(path.join(process.env.OPENAI_CONSUMER_DIR, 'package.json'));
const OpenAI = req('openai');
const { OpenAIInstrumentation } = req('@traceai/openai');
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = req(
  '@opentelemetry/sdk-trace-base',
);
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let inst;
function instrument(traceConfig = {}) {
  inst = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig });
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
const response = {
  id: 'resp-synthetic',
  object: 'response',
  created_at: 1,
  status: 'completed',
  model: 'synthetic',
  output: [],
  usage: { input_tokens: 2, output_tokens: 3, total_tokens: 5 },
};
function fixture(payload, stream = false) {
  let wire;
  const client = new OpenAI({
    apiKey: 'synthetic-not-a-key',
    maxRetries: 0,
    fetch: async (_url, options) => {
      wire = JSON.parse(options.body);
      return new Response(
        stream
          ? payload.map((x) => 'data: ' + JSON.stringify(x) + '\n\n').join('') + 'data: [DONE]\n\n'
          : JSON.stringify(payload),
        { headers: { 'content-type': stream ? 'text/event-stream' : 'application/json' } },
      );
    },
  });
  return { client, wire: () => wire };
}
function attrs() {
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  return spans[0].attributes;
}
function noMarker(marker) {
  const a = attrs();
  assert.deepEqual(
    Object.keys(a).filter((k) => JSON.stringify(a[k]).includes(marker)),
    [],
    'credential-bearing attributes',
  );
}
if (OpenAI.Responses) {
  for (const hidden of [false, true])
    test(`Responses nested MCP authorization: capture ${hidden ? 'disabled' : 'enabled'}`, async () => {
      const marker = 'SYNTHETIC-NESTED-AUTH-CREDENTIAL';
      const body = {
        model: 'synthetic',
        input: [
          {
            type: 'tool_search_output',
            call_id: 'call_search_synthetic',
            execution: 'client',
            tools: [
              {
                type: 'mcp',
                server_label: 'synthetic',
                server_url: 'https://example.invalid/mcp',
                authorization: marker,
                headers: { 'X-Api-Key': marker },
              },
            ],
          },
        ],
      };
      const before = structuredClone(body);
      const { client, wire } = fixture(response);
      const baseline = await client.responses.create(body);
      instrument({ hideInputs: hidden, hideOutputs: hidden });
      assert.deepEqual(await client.responses.create(body), baseline);
      assert.deepEqual(body, before);
      assert.deepEqual(wire(), before);
      noMarker(marker);
    });
  for (const location of ['userinfo', 'query'])
    for (const hidden of [false, true])
      test(`Responses MCP URL ${location} credentials: capture ${hidden ? 'disabled' : 'enabled'}`, async () => {
        const marker = 'SYNTHETIC-URL-CREDENTIAL';
        const server_url =
          location === 'userinfo'
            ? 'https://synthetic:' + marker + '@example.invalid/mcp'
            : 'https://example.invalid/mcp?api_key=' + marker;
        const body = {
          model: 'synthetic',
          input: 'hello',
          tools: [{ type: 'mcp', server_label: 'synthetic', server_url }],
        };
        const before = structuredClone(body);
        const { client, wire } = fixture(response);
        const baseline = await client.responses.create(body);
        instrument({ hideInputs: hidden, hideOutputs: hidden });
        assert.deepEqual(await client.responses.create(body), baseline);
        assert.deepEqual(body, before);
        assert.deepEqual(wire(), before);
        noMarker(marker);
      });
  const custom = {
    type: 'custom_tool_call',
    id: 'ctc_synthetic',
    call_id: 'call_custom_synthetic',
    name: 'code_exec',
    input: 'print(1)',
    status: 'completed',
  };
  for (const stream of [false, true])
    test(`Responses ${stream ? 'stream' : 'nonstream'} custom tool output is not an empty message`, async () => {
      const payload = { ...response, output: [custom] };
      const { client } = fixture(
        stream ? [{ type: 'response.completed', sequence_number: 0, response: payload }] : payload,
        stream,
      );
      const body = {
        model: 'synthetic',
        input: 'execute a calculation',
        tools: [{ type: 'custom', name: 'code_exec' }],
        ...(stream ? { stream: true } : {}),
      };
      async function invoke() {
        const v = await client.responses.create(body);
        if (!stream) return v;
        const chunks = [];
        for await (const x of v) chunks.push(x);
        return chunks;
      }
      const baseline = await invoke();
      instrument();
      assert.deepEqual(await invoke(), baseline);
      const messages = JSON.parse(attrs()['gen_ai.output.messages']);
      assert.notDeepEqual(messages, [{}], 'custom tool output is erased');
      assert.ok(
        JSON.stringify(messages).includes('code_exec'),
        'structured output must identify custom tool',
      );
      assert.ok(
        JSON.stringify(messages).includes('print(1)'),
        'structured output must retain custom input',
      );
      assert.ok(
        JSON.stringify(messages).includes('call_custom_synthetic'),
        'structured output must retain tool call identity',
      );
    });
  test('Responses custom tool history retains call/output relationship', async () => {
    const body = {
      model: 'synthetic',
      input: [custom, { type: 'custom_tool_call_output', call_id: custom.call_id, output: '1' }],
    };
    const before = structuredClone(body);
    const { client, wire } = fixture(response);
    const baseline = await client.responses.create(body);
    instrument();
    assert.deepEqual(await client.responses.create(body), baseline);
    assert.deepEqual(wire(), before);
    assert.deepEqual(body, before);
    const messages = JSON.parse(attrs()['gen_ai.input.messages']);
    assert.notDeepEqual(messages, [{}, {}], 'custom tool history is erased');
    assert.ok(
      JSON.stringify(messages).includes(custom.call_id),
      'structured history must preserve call identity',
    );
    assert.ok(
      JSON.stringify(messages).includes('print(1)'),
      'structured history must preserve custom input',
    );
    assert.ok(
      JSON.stringify(messages).includes('"1"'),
      'structured history must preserve tool output',
    );
  });
}
if (OpenAI.Responses) {
  for (const [setting, direction] of [
    ['hideInputText', 'input'],
    ['hideOutputText', 'output'],
  ]) {
    test(`Responses custom tool ${direction} payload respects ${setting}`, async () => {
      const marker = 'SYNTHETIC-CUSTOM-TOOL-PRIVATE';
      const call = {
        type: 'custom_tool_call',
        id: 'ctc_private',
        call_id: 'call_private',
        name: 'code_exec',
        input: marker,
        status: 'completed',
      };
      const body = {
        model: 'synthetic',
        input:
          direction === 'input'
            ? [call, { type: 'custom_tool_call_output', call_id: call.call_id, output: marker }]
            : 'hello',
      };
      const { client } = fixture({ ...response, output: direction === 'output' ? [call] : [] });
      instrument({ [setting]: true });
      await client.responses.create(body);
      const messages = attrs()[`gen_ai.${direction}.messages`];
      assert.ok(messages, 'semantic messages should remain available');
      assert.ok(
        !messages.includes(marker),
        'private payload must be hidden in structured messages',
      );
      assert.ok(messages.includes('assistant'), 'message role must remain available');
    });
  }
  test('Responses MCP URL malformed and fragment credentials fail closed', async () => {
    const marker = 'SYNTHETIC-URL-SECRET';
    const { client } = fixture(response);
    instrument();
    await client.responses.create({
      model: 'synthetic',
      input: 'hello',
      tools: [
        { type: 'mcp', server_label: 'frag', server_url: 'https://example.invalid/mcp#' + marker },
        { type: 'mcp', server_label: 'bad', server_url: 'not-a-url/' + marker },
        {
          type: 'mcp',
          server_label: 'path',
          server_url: 'https://example.invalid/' + marker + '/mcp',
        },
      ],
    });
    noMarker(marker);
  });
}
