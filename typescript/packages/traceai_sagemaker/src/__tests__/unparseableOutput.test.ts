import { bytes, client, ENDPOINT, instrument, dumpOf } from "./harness";

const REQ = "REQUEST-MARK-7a";
const ECHO = "ECHOED-REQUEST-MARK-8b";
const lib = "@aws-sdk/client-sagemaker-runtime" as const;
const INPUT_SETTINGS = ["hideInputs", "hideInputText", "hideInputMessages", "hideInputImages"];

const BODIES: Record<string, () => string> = {
  jsonlines: () => `{"prompt":"${ECHO}"}\n{"index":2}\n`,
  "malformed JSON": () => `prompt: ${ECHO} {"tail": `,
  "over the parse limit": () => `${JSON.stringify({ prompt: ECHO })}${" ".repeat((1 << 20) + 64)}`,
};

async function run(traceConfig: Record<string, boolean>, body: string) {
  const ctx = instrument(lib, traceConfig);
  const c = client(ctx.lib, () => ({ Body: bytes(body), ContentType: "application/json", $metadata: {} }));
  await c.send(new ctx.lib.InvokeEndpointCommand({ EndpointName: ENDPOINT, ContentType: "application/json", Body: bytes({ prompt: REQ }) }));
  ctx.instrumentation.disable();
  const spans = ctx.spans();
  await ctx.provider.shutdown();
  return { spans, dump: dumpOf(spans) };
}

describe.each(Object.keys(BODIES))("response that is %s", (name) => {
  it("is recorded as written with no hide setting (control)", async () => {
    const { dump } = await run({}, BODIES[name]());
    expect(dump).toContain(ECHO);
  });

  it.each(INPUT_SETTINGS)("%s keeps echoed request fields out of every attribute", async (setting) => {
    const { dump } = await run({ [setting]: true }, BODIES[name]());
    expect(dump).not.toContain(ECHO);
    expect(dump).not.toContain(REQ);
  });
});
