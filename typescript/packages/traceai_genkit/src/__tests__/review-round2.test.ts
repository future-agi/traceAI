import { mapGenkitAttributes, SHOW_ALL } from "../index";
import { generateAttributes, modelAttributes } from "./fixtures";

const marker = "SYNTHETIC_PRIVATE_TRUNCATION_MARKER";
const request = { model: "offline/model", messages: [{ role: "user", content: [{ text: marker + " ".repeat(1500) }] }] };
const response = { message: { role: "model", content: [{ text: "public reply" }] }, request };

it.each(["hideInputs", "hideInputMessages", "hideInputText"] as const)(
  "fails closed on a truncated custom generate request and response with %s", (flag) => {
    const input = JSON.stringify(request).slice(0, 256);
    const output = JSON.stringify(response).slice(0, 256);
    expect(() => JSON.parse(input)).toThrow();
    expect(() => JSON.parse(output)).toThrow();
    const source = { ...generateAttributes(), "genkit:name": "custom-generation-step", "genkit:input": input, "genkit:output": output };
    const mapped = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, [flag]: true } });
    expect(JSON.stringify(mapped)).not.toContain(marker);
    expect(mapped["genkit:output"]).toBe("__REDACTED__");
    expect(mapped["output.value"]).toBe("__REDACTED__");
    expect(source["genkit:output"]).toContain(marker);
  },
);

it("preserves a non-generation util response if its input is parseable", () => {
  const arbitrary = { ...generateAttributes(), "genkit:name": "other", "genkit:input": JSON.stringify({ query: "other" }), "genkit:output": JSON.stringify(response) };
  expect(mapGenkitAttributes(arbitrary, { policy: { ...SHOW_ALL, hideInputs: true } })["genkit:output"]).toEqual(arbitrary["genkit:output"]);
});

it.each([false, true])("maps completed background Operation.output usage with hideOutputs=%s", (hideOutputs) => {
  const operation = { id: "public-id", done: true, output: {
    message: { role: "model", content: [{ text: "reply" }] }, finishReason: "stop",
    usage: { inputTokens: 13, outputTokens: 7, totalTokens: 20 },
  } };
  const source = { ...modelAttributes(), "genkit:metadata:subtype": "background-model", "genkit:output": JSON.stringify(operation) };
  const mapped = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, hideOutputs } });
  expect(mapped["gen_ai.usage.input_tokens"]).toBe(13);
  expect(mapped["gen_ai.usage.output_tokens"]).toBe(7);
  expect(mapped["gen_ai.usage.total_tokens"]).toBe(20);
  expect(mapped["gen_ai.response.finish_reasons"]).toEqual(["stop"]);
  expect(mapGenkitAttributes({ ...generateAttributes(), "genkit:output": JSON.stringify({ operation }) })["gen_ai.usage.total_tokens"]).toBeUndefined();
});
