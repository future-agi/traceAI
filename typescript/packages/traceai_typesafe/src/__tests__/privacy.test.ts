import { TypeSafeInstrumentation } from "../instrumentation";
import { BASE_URL, fakeFetch, SECRET_KEY, sdk, tracing } from "./harness";

const lib = sdk("@typesafe-ai/sdk");
const PROMPT = "PROMPT-MARK-1f", INSTRUCTION = "INSTRUCTION-MARK-2e", LABEL = "angry-LABEL-3d", REASON = "ERROR-ECHO-4c";
const REDACTED = "__REDACTED__";

async function run(traceConfig: Record<string, boolean>, status = 200) {
  const t = tracing();
  const instrumentation = new TypeSafeInstrumentation({ traceConfig });
  instrumentation.setTracerProvider(t.provider);
  instrumentation.manuallyInstrument(lib as unknown as Record<string, unknown>);
  const { fetch } = fakeFetch([status === 200
    ? { body: { model: "m", answers: { tone: { type: "choice", choice: LABEL, confidence: 0.9, probabilities: {} }, billing: { type: "noul", noul: 0.5 } }, usage: { input_tokens: 1, output_tokens: 1 } } }
    : { status, body: { detail: { error_type: "oops", message: REASON } } }]);
  const client = new lib.TypeSafeClient({ apiKey: SECRET_KEY, baseURL: BASE_URL, fetch, retry: { maxRetries: 0 } });
  try {
    await client.systemOne({
      state: PROMPT,
      questions: { tone: lib.choice(INSTRUCTION, { [LABEL]: null, other: null }), billing: lib.noul("billing?") },
    }, { headers: { "Idempotency-Key": "idem-private" } });
  } catch { /* the error path is asserted by the dump */ }
  instrumentation.disable();
  const spans = t.spans();
  await t.provider.shutdown();
  return { spans, dump: JSON.stringify(spans.map((s) => ({ a: s.attributes, e: s.events, s: s.status }))) };
}

describe("TypeSafe privacy through the fi-core masker", () => {
  it("exports prompt, instructions and decisions with no hide setting (control)", async () => {
    const { dump, spans } = await run({});
    for (const marker of [PROMPT, INSTRUCTION, LABEL, "idem-private"]) expect(dump).toContain(marker);
    expect(spans[0].attributes["typesafe.question.billing.noul"]).toBe(0.5);
  });

  it.each([["hideInputs"], ["hideInputText"]])("%s removes the prompt, instructions and idempotency key", async (flag) => {
    const { dump, spans } = await run({ [flag]: true });
    for (const marker of [PROMPT, INSTRUCTION, "idem-private"]) expect(dump).not.toContain(marker);
    expect(spans[0].attributes["input.value"] === undefined || spans[0].attributes["input.value"] === REDACTED).toBe(true);
    expect(dump).toContain(LABEL);
    expect(spans[0].attributes["typesafe.question.tone.type"]).toBe("choice");
  });

  it.each([["hideOutputs"], ["hideOutputText"]])("%s removes the decisions' labels and keeps numeric answers", async (flag) => {
    const { dump, spans } = await run({ [flag]: true });
    expect(dump).not.toContain(LABEL);
    expect(dump).toContain(PROMPT);
    expect(spans[0].attributes["output.value"] === undefined || spans[0].attributes["output.value"] === REDACTED).toBe(true);
    expect(spans[0].attributes["typesafe.question.billing.noul"]).toBe(0.5);
  });

  it("keeps the server's error text only while neither side is hidden", async () => {
    expect((await run({}, 400)).dump).toContain(REASON);
    expect((await run({ hideInputs: true }, 400)).dump).not.toContain(REASON);
    expect((await run({ hideOutputs: true }, 400)).dump).not.toContain(REASON);
  });

  it("never exports the API key", async () => {
    for (const config of [{}, { hideInputs: true, hideOutputs: true }]) expect((await run(config)).dump).not.toContain(SECRET_KEY);
  });
});
