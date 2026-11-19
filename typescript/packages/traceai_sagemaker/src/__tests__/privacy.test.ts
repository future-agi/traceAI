import { bytes, client, ENDPOINT, events, instrument, SDKS, dumpOf } from "./harness";

const REQ = "REQUEST-MARK-1f";
const REQ_STOP = "STOP-SEQUENCE-MARK-2e";
const RESP = "RESPONSE-MARK-3d";
const ECHO = "ECHOED-REQUEST-MARK-4c";
const ERR = "ERROR-ECHO-MARK-5b";
const lib = "@aws-sdk/client-sagemaker-runtime" as const;

const INPUT_SETTINGS = ["hideInputs", "hideInputText", "hideInputMessages", "hideInputImages"];
const OUTPUT_SETTINGS = ["hideOutputs", "hideOutputText", "hideOutputMessages"];

type Body = unknown;
async function run(traceConfig: Record<string, boolean>, opts: { request?: Body; response?: Body; fail?: boolean; stream?: boolean } = {}) {
  const ctx = instrument(lib, traceConfig);
  const request = opts.request ?? { messages: [{ role: "user", content: REQ }], stop: [REQ_STOP], max_tokens: 8 };
  const response = opts.response ?? { choices: [{ message: { content: RESP } }], echo: ECHO, usage: { prompt_tokens: 1, completion_tokens: 2 } };
  const c = client(ctx.lib, () => {
    if (opts.fail) throw Object.assign(new Error(`${ERR} ${REQ}`), { name: "ModelError" });
    if (opts.stream) return { Body: events([`data: ${JSON.stringify({ choices: [{ delta: { content: RESP } }] })}\n`]), ContentType: "text/event-stream", $metadata: {} };
    return { Body: bytes(response), ContentType: "application/json", $metadata: {} };
  });
  const Command = opts.stream ? ctx.lib.InvokeEndpointWithResponseStreamCommand : ctx.lib.InvokeEndpointCommand;
  try {
    const out = await c.send(new Command({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes(request) }));
    if (opts.stream) for await (const _ of out.Body) { /* drain */ }
  } catch { /* the error path is asserted through the dump */ }
  ctx.instrumentation.disable();
  const spans = ctx.spans();
  await ctx.provider.shutdown();
  return { spans, dump: dumpOf(spans) };
}

describe("SageMaker Runtime privacy through the fi-core masker", () => {
  it("records request, parameters and response with no hide setting (control)", async () => {
    const { dump, spans } = await run({});
    for (const mark of [REQ, REQ_STOP, RESP]) expect(dump).toContain(mark);
    expect(spans[0].attributes["output.value"]).toBe(RESP);
  });

  it.each(INPUT_SETTINGS)("%s keeps the request and its parameters out of every attribute", async (setting) => {
    const { dump, spans } = await run({ [setting]: true });
    for (const mark of [REQ, REQ_STOP]) expect(dump).not.toContain(mark);
    expect(dump).toContain(RESP);
    expect(spans[0].attributes["gen_ai.usage.output_tokens"]).toBe(2);
    expect(spans[0].attributes["gen_ai.request.model"]).toBe(ENDPOINT);
  });

  it.each(OUTPUT_SETTINGS)("%s keeps the response out of every attribute", async (setting) => {
    const { dump, spans } = await run({ [setting]: true });
    expect(dump).not.toContain(RESP);
    expect(dump).toContain(REQ);
    expect(spans[0].attributes["gen_ai.usage.output_tokens"]).toBe(2);
  });

  it.each(OUTPUT_SETTINGS)("%s keeps streamed text out of every attribute", async (setting) => {
    const { dump } = await run({ [setting]: true }, { stream: true });
    expect(dump).not.toContain(RESP);
  });

  it("records streamed text with no hide setting (control)", async () => {
    const { spans } = await run({}, { stream: true });
    expect(spans[0].attributes["output.value"]).toBe(RESP);
  });

  it.each([["number", 31337, "31337"], ["boolean", true, "true"]])(
    "a %s request body is recorded with no setting and absent under each input setting",
    async (_kind, value, text) => {
      expect((await run({}, { request: value })).spans[0].attributes["input.value"]).toBe(text);
      for (const setting of INPUT_SETTINGS) {
        const { spans } = await run({ [setting]: true }, { request: value });
        expect(["__REDACTED__", undefined]).toContain(spans[0].attributes["input.value"]);
        expect(spans[0].attributes["input.value"]).not.toBe(text);
      }
    },
  );

  it.each([["number", 4242, "4242"], ["boolean", false, "false"]])(
    "a %s response body is recorded with no setting and absent under each setting",
    async (_kind, value, text) => {
      expect((await run({}, { response: value })).spans[0].attributes["output.value"]).toBe(text);
      for (const setting of [...INPUT_SETTINGS, ...OUTPUT_SETTINGS]) {
        const { spans } = await run({ [setting]: true }, { response: value });
        expect(spans[0].attributes["output.value"]).not.toBe(text);
      }
    },
  );

  it.each([...INPUT_SETTINGS, ...OUTPUT_SETTINGS])(
    "%s never writes the whole response object or a raw copy of either side",
    async (setting) => {
      const { spans, dump } = await run({ [setting]: true }, { response: { predictions: [0.5], echoed_request: ECHO } });
      expect(dump).not.toContain(ECHO);
      expect(spans[0].attributes["raw.input"]).toBeUndefined();
      expect(spans[0].attributes["raw.output"]).toBeUndefined();
      expect(spans[0].attributes["output.value"]).toBeUndefined();
    },
  );

  it("never writes a raw copy of either side, even with no hide setting", async () => {
    const { spans } = await run({});
    expect(spans[0].attributes["raw.input"]).toBeUndefined();
    expect(spans[0].attributes["raw.output"]).toBeUndefined();
  });

  it.each([{}, ...INPUT_SETTINGS.concat(OUTPUT_SETTINGS).map((s) => ({ [s]: true }))])(
    "error text carrying request content is not recorded (%j)",
    async (cfg) => {
      const { dump, spans } = await run(cfg as Record<string, boolean>, { fail: true });
      expect(dump).not.toContain(ERR);
      expect(spans[0].attributes["error.type"]).toBe("ModelError");
    },
  );

  it("covers both SDK copies", () => expect(SDKS).toHaveLength(2));
});
