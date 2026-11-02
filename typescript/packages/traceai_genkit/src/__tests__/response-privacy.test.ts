import { mapGenkitAttributes, SHOW_ALL, type ContentPolicy } from "../index";
import { generateAttributes, modelAttributes, toolAttributes } from "./fixtures";

const prompt = "SYNTHETIC_HIDDEN_PROMPT";
const error = "SYNTHETIC_PRIVATE_ERROR";
const reply = "SYNTHETIC_PUBLIC_REPLY";
const request = { messages: [{ role: "user", content: [{ text: prompt }, { reasoning: prompt }] }], config: { temperature: 0 } };
const response = {
  message: { role: "model", content: [{ text: reply }] },
  finishReason: "failed",
  finishMessage: error,
  error: { status: "INTERNAL", message: error, details: { private: error } },
  usage: { inputTokens: 2, outputTokens: 1, totalTokens: 3 },
  request,
};

function mapped(policy: Partial<ContentPolicy>, attributes = generateAttributes(), value: unknown = response) {
  const source = { ...attributes, "genkit:input": JSON.stringify(request), "genkit:output": JSON.stringify(value) };
  const before = JSON.stringify(source);
  const out = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, ...policy } });
  expect(JSON.stringify(source)).toBe(before);
  expect(out["genkit:output"]).toBe(out["output.value"]);
  return { out, output: JSON.parse(out["genkit:output"] as string) };
}

describe.each([generateAttributes, modelAttributes])("generation response envelope privacy (%#)", (attributes) => {
  it.each(["hideErrorDetails", "hideInputs", "hideInputMessages", "hideInputText", "hideOutputText"] as const)(
    "%s removes serialized error details, retaining status, finish reason and usage", (key) => {
      const { output, out } = mapped({ [key]: true }, attributes());
      expect(JSON.stringify(out)).not.toContain(error);
      expect(output.error).toEqual({ status: "INTERNAL" });
      expect(output.finishReason).toBe("failed");
      expect(output.usage).toEqual(response.usage);
    },
  );

  it.each(["hideInputs", "hideInputMessages"] as const)("%s masks the embedded request without losing output", (key) => {
    const { output, out } = mapped({ [key]: true }, attributes());
    expect(JSON.stringify(out)).not.toContain(prompt);
    expect(output.request).toBeUndefined();
    expect(output.message).toEqual(response.message);
  });

  it("input text masking preserves request roles/config and public model output", () => {
    const { output, out } = mapped({ hideInputText: true }, attributes());
    expect(JSON.stringify(out)).not.toContain(prompt);
    expect(output.request.messages[0]).toEqual({ role: "user", content: [{ text: "__REDACTED__" }, { reasoning: "__REDACTED__" }] });
    expect(output.request.config).toEqual(request.config);
    expect(output.message).toEqual(response.message);
  });

  it("an explicit error opt-out does not override input privacy", () => {
    const { output, out } = mapped({ hideInputs: true, hideErrorDetails: false }, attributes());
    expect(JSON.stringify(out)).not.toContain(prompt);
    expect(output.error).toEqual(response.error);
    expect(output.finishMessage).toBe(error);
  });

  it("leaves content intact by default", () => {
    expect(mapped({}, attributes()).output).toEqual(response);
  });

  it("fails closed on a truncated native response when redaction is required", () => {
    const source = { ...attributes(), "genkit:output": '{"finishMessage":"SYNTHETIC_PRIVATE_ERROR' };
    for (const policy of [{ hideErrorDetails: true }, { hideInputs: true }, { hideInputText: true }]) {
      const out = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, ...policy } });
      expect(out["genkit:output"]).toBe("__REDACTED__");
      expect(out["output.value"]).toBe("__REDACTED__");
    }
  });
});

it("does not interpret arbitrary tool output as a generation envelope", () => {
  expect(mapped({ hideErrorDetails: true, hideInputs: true }, toolAttributes()).output).toEqual(response);
});

it("sanitizes deprecated candidate finish messages without removing model messages", () => {
  const value = { candidates: [{ index: 0, finishReason: "blocked", finishMessage: error, message: response.message }], request };
  const { output } = mapped({ hideErrorDetails: true, hideInputs: true }, modelAttributes(), value);
  expect(JSON.stringify(output)).not.toContain(error);
  expect(output.request).toBeUndefined();
  expect(output.candidates[0]).toEqual({ index: 0, finishReason: "blocked", message: response.message });
});
