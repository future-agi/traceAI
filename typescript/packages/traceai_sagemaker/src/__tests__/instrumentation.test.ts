import { bytes, client, ENDPOINT, events, instrument, loopback, SDKS, SdkName } from "./harness";

const PROMPT = "PROMPT-TEXT-a1";
const OUT = "OUTPUT-TEXT-b2";
const reply = (body: unknown, extra: Record<string, unknown> = {}) => ({
  Body: bytes(body), ContentType: "application/json", InvokedProductionVariant: "variant-1",
  $metadata: { httpStatusCode: 200, requestId: "req-1" }, ...extra,
});
const chat = { choices: [{ message: { role: "assistant", content: OUT } }], model: "m-1", usage: { prompt_tokens: 3, completion_tokens: 4, total_tokens: 7 } };

describe.each(SDKS)("SageMaker Runtime instrumentation (%s)", (name: SdkName) => {
  let ctx: ReturnType<typeof instrument>;
  beforeEach(() => { ctx = instrument(name); });
  afterEach(async () => { ctx.instrumentation.disable(); await ctx.provider.shutdown(); });

  it("InvokeEndpoint: one LLM span with request, response and usage", async () => {
    const c = client(ctx.lib, () => reply(chat));
    const out = await c.send(new ctx.lib.InvokeEndpointCommand({
      EndpointName: ENDPOINT, InferenceComponentName: "ic-1", TargetVariant: "variant-1", InferenceId: "inf-1",
      ContentType: "application/json", Body: bytes({ messages: [{ role: "user", content: PROMPT }], max_tokens: 16, temperature: 0.5 }),
    }));
    expect(out.InvokedProductionVariant).toBe("variant-1");
    const [span, ...rest] = ctx.spans();
    expect(rest).toHaveLength(0);
    expect(span.name).toBe("sagemaker.invoke_endpoint");
    const a = span.attributes;
    expect(a["fi.span.kind"]).toBe("LLM");
    expect(a["gen_ai.provider.name"]).toBe("aws.sagemaker");
    expect(a["gen_ai.operation.name"]).toBe("invoke_endpoint");
    expect(a["gen_ai.request.model"]).toBe(ENDPOINT);
    expect(a["aws.sagemaker.endpoint_name"]).toBe(ENDPOINT);
    expect(a["aws.sagemaker.inference_component"]).toBe("ic-1");
    expect(a["aws.sagemaker.target_variant"]).toBe("variant-1");
    expect(a["aws.sagemaker.inference_id"]).toBe("inf-1");
    expect(a["aws.sagemaker.invoked_variant"]).toBe("variant-1");
    expect(a["aws.request_id"]).toBe("req-1");
    expect(String(a["input.value"])).toContain(PROMPT);
    expect(a["input.mime_type"]).toBe("application/json");
    expect(JSON.parse(String(a["gen_ai.request.parameters"]))).toEqual({ max_tokens: 16, temperature: 0.5 });
    expect(a["output.value"]).toBe(OUT);
    expect(a["output.mime_type"]).toBe("text/plain");
    expect(a["gen_ai.response.model"]).toBe("m-1");
    expect(a["gen_ai.usage.input_tokens"]).toBe(3);
    expect(a["gen_ai.usage.output_tokens"]).toBe(4);
    expect(a["gen_ai.usage.total_tokens"]).toBe(7);
    expect(span.status.code).toBe(1);
  });

  it("keeps a zero output token count", async () => {
    const c = client(ctx.lib, () => reply({ ...chat, usage: { prompt_tokens: 3, completion_tokens: 0 } }));
    await c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ inputs: "x" }) }));
    const a = ctx.spans()[0].attributes;
    expect(a["gen_ai.usage.output_tokens"]).toBe(0);
    expect(a["gen_ai.usage.total_tokens"]).toBe(3);
  });

  it("notes binary request and response bodies by type and size, never as numeric bytes", async () => {
    const payload = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]);
    const c = client(ctx.lib, () => ({ Body: Uint8Array.from([1, 2, 3, 4, 5]), ContentType: "application/octet-stream", $metadata: {} }));
    await c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/x-image", Body: payload }));
    const a = ctx.spans()[0].attributes;
    expect(a["input.value"]).toBe("[binary content: application/x-image, 8 bytes]");
    expect(a["output.value"]).toBe("[binary content: application/octet-stream, 5 bytes]");
    expect(JSON.stringify(a)).not.toMatch(/137,\s*80|\[1,\s*2,\s*3/);
  });

  it("records the whole body when the output has no recognised text field", async () => {
    const c = client(ctx.lib, () => reply({ predictions: [0.25, 0.75] }));
    await c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ instances: [1] }) }));
    expect(ctx.spans()[0].attributes["output.value"]).toBe('{"predictions":[0.25,0.75]}');
    expect(ctx.spans()[0].attributes["output.mime_type"]).toBe("application/json");
  });

  it("InvokeEndpointAsync: records the inference id, no body", async () => {
    const c = client(ctx.lib, () => ({ InferenceId: "async-1", OutputLocation: "s3://bucket/out", $metadata: { requestId: "req-2" } }));
    await c.send(new ctx.lib.InvokeEndpointAsyncCommand({ EndpointName: ENDPOINT, InputLocation: "s3://bucket/in", ContentType: "application/json" }));
    const [span] = ctx.spans();
    expect(span.name).toBe("sagemaker.invoke_endpoint_async");
    expect(span.attributes["aws.sagemaker.inference_id"]).toBe("async-1");
    expect(span.attributes["input.value"]).toBeUndefined();
    expect(span.attributes["output.value"]).toBeUndefined();
    expect(JSON.stringify(span.attributes)).not.toContain("s3://");
  });

  describe("InvokeEndpointWithResponseStream", () => {
    const sse = (text: string) => `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n`;
    const command = (lib: typeof ctx.lib) => new lib.InvokeEndpointWithResponseStreamCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ stream: true }) });

    it("aggregates text across parts, forwards every event unchanged, ends once", async () => {
      const line = sse("Hel") + sse("lo ");
      const parts = [line.slice(0, 20), line.slice(20), sse("there") + 'data: {"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5},"model":"m-2"}\ndata: [DONE]\n'];
      const sent: unknown[] = [];
      const c = client(ctx.lib, async () => {
        const source = events(parts);
        return { Body: (async function* () { for await (const e of source) { sent.push(e); yield e; } })(), ContentType: "text/event-stream", InvokedProductionVariant: "v-s", $metadata: { requestId: "req-3" } };
      });
      const out = await c.send(command(ctx.lib));
      expect(ctx.spans()).toHaveLength(0);
      const received: unknown[] = [];
      for await (const e of out.Body) received.push(e);
      expect(received).toEqual(sent);
      received.forEach((e, i) => expect(e).toBe(sent[i]));
      const [span, ...rest] = ctx.spans();
      expect(rest).toHaveLength(0);
      expect(span.name).toBe("sagemaker.invoke_endpoint_with_response_stream");
      expect(span.attributes["output.value"]).toBe("Hello there");
      expect(span.attributes["gen_ai.usage.total_tokens"]).toBe(5);
      expect(span.attributes["gen_ai.usage.output_tokens"]).toBe(3);
      expect(span.attributes["gen_ai.response.model"]).toBe("m-2");
      expect(span.attributes["aws.sagemaker.invoked_variant"]).toBe("v-s");
      expect(typeof span.attributes["gen_ai.server.time_to_first_token"]).toBe("number");
      expect(span.attributes["aws.sagemaker.stream.incomplete"]).toBeUndefined();
      expect(span.status.code).toBe(1);
    });

    it("uses the final generated text of a token stream", async () => {
      const lines = ['{"token":{"text":"a"}}\n', '{"token":{"text":"b"},"generated_text":"ab","details":{"generated_tokens":2}}\n'];
      const c = client(ctx.lib, () => ({ Body: events(lines), ContentType: "application/jsonlines", $metadata: {} }));
      const out = await c.send(command(ctx.lib));
      for await (const _ of out.Body) { /* drain */ }
      const a = ctx.spans()[0].attributes;
      expect(a["output.value"]).toBe("ab");
      expect(a["gen_ai.usage.output_tokens"]).toBe(2);
    });

    it("ends once, marked incomplete, when the consumer stops early", async () => {
      const c = client(ctx.lib, () => ({ Body: events([sse("one"), sse("two"), sse("three")]), ContentType: "text/event-stream", $metadata: {} }));
      const out = await c.send(command(ctx.lib));
      for await (const _ of out.Body) break;
      for await (const _ of out.Body) break; // a second early exit must not end the span again
      const spans = ctx.spans();
      expect(spans).toHaveLength(1);
      expect(spans[0].attributes["aws.sagemaker.stream.incomplete"]).toBe(true);
    });

    it("records the error class once when the stream fails, and rethrows the same error", async () => {
      const failure = Object.assign(new Error(`model said ${PROMPT}`), { name: "ModelStreamError" });
      const c = client(ctx.lib, () => ({ Body: events([sse("x")], failure), ContentType: "text/event-stream", $metadata: {} }));
      const out = await c.send(command(ctx.lib));
      let caught: unknown;
      try { for await (const _ of out.Body) { /* drain */ } } catch (e) { caught = e; }
      expect(caught).toBe(failure);
      const spans = ctx.spans();
      expect(spans).toHaveLength(1);
      expect(spans[0].attributes["error.type"]).toBe("ModelStreamError");
      expect(spans[0].status.code).toBe(2);
      expect(JSON.stringify([spans[0].status, spans[0].events, spans[0].attributes])).not.toContain(PROMPT);
    });

    it("notes a binary stream by type and size", async () => {
      const c = client(ctx.lib, () => ({ Body: events([Uint8Array.from([9, 9, 9]), Uint8Array.from([8, 8])]), ContentType: "application/octet-stream", $metadata: {} }));
      const out = await c.send(command(ctx.lib));
      for await (const _ of out.Body) { /* drain */ }
      expect(ctx.spans()[0].attributes["output.value"]).toBe("[binary content: application/octet-stream, 5 bytes]");
    });
  });

  it("records the error class once on a failed call and rethrows the same error without its message", async () => {
    const failure = Object.assign(new Error(`rejected: ${PROMPT}`), { name: "ModelError" });
    const c = client(ctx.lib, () => { throw failure; });
    await expect(c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ inputs: "x" }) }))).rejects.toBe(failure);
    const spans = ctx.spans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["error.type"]).toBe("ModelError");
    expect(spans[0].status.code).toBe(2);
    expect(JSON.stringify([spans[0].status, spans[0].events])).not.toContain(PROMPT);
  });

  it("supports the callback overload and ends the span once", async () => {
    const c = client(ctx.lib, () => reply(chat));
    const result = await new Promise<{ Body: Uint8Array }>((resolve, reject) =>
      c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ inputs: "x" }) }),
        (error: unknown, data: { Body: Uint8Array }) => (error ? reject(error) : resolve(data))));
    expect(new TextDecoder().decode(result.Body)).toContain(OUT);
    expect(ctx.spans()).toHaveLength(1);
    expect(ctx.spans()[0].attributes["output.value"]).toBe(OUT);
  });

  it("leaves the response body readable by the caller", async () => {
    const server = await loopback(() => ({ headers: { "content-type": "application/json", "x-amzn-invoked-production-variant": "v-http" }, body: JSON.stringify(chat) }));
    try {
      const c = new ctx.lib.SageMakerRuntimeClient({ region: "us-east-1", endpoint: server.url, credentials: { accessKeyId: "testing", secretAccessKey: "testing" } });
      const out = await c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ inputs: PROMPT }) }));
      expect(JSON.parse(new TextDecoder().decode(out.Body))).toEqual(chat);
      expect(server.seen[0].body).toContain(PROMPT);
      const [span] = ctx.spans();
      expect(span.attributes["aws.sagemaker.invoked_variant"]).toBe("v-http");
      expect(span.attributes["output.value"]).toBe(OUT);
    } finally { await server.close(); }
  });

  it("does nothing once disabled and passes other commands through", async () => {
    const c = client(ctx.lib, () => reply(chat));
    ctx.instrumentation.disable();
    await c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ inputs: "x" }) }));
    expect(ctx.spans()).toHaveLength(0);
  });
});
