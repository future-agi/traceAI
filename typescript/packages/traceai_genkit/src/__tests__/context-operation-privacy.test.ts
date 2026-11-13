import { mapGenkitAttributes, SHOW_ALL } from "../index";
import { flowAttributes, generateAttributes, modelAttributes } from "./fixtures";

const marker = "SYNTHETIC_CONTEXT_OPERATION_CONTENT";
const inputFlags = ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"] as const;
const outputFlags = ["hideOutputs", "hideOutputMessages", "hideOutputText"] as const;

it.each(inputFlags)("masks context input copies with %s", (flag) => {
  const contextAttributes = {
    "llm.prompt_template.variables": JSON.stringify({ value: marker }),
    "input.value": marker,
    "raw.input": marker,
    "gen_ai.input.messages": JSON.stringify([{ role: "user", content: [
      { type: "text", text: flag === "hideInputImages" ? "public" : marker },
      { type: "image_url", image_url: { url: `https://example.invalid/${marker}` } },
    ] }]),
  };
  // Isolate the input-text case from intentionally visible images.
  if (flag === "hideInputText") contextAttributes["gen_ai.input.messages"] = JSON.stringify([{ role: "user", content: marker }]);
  const source = flowAttributes();
  const original = JSON.stringify({ source, contextAttributes });
  const result = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, [flag]: true }, contextAttributes });
  expect(JSON.stringify(result)).not.toContain(marker);
  expect(result["input.value"]).toBe("__REDACTED__");
  expect(result["llm.prompt_template.variables"]).toBe("__REDACTED__");
  expect(JSON.stringify({ source, contextAttributes })).toBe(original);
  expect(JSON.stringify(mapGenkitAttributes(source, { contextAttributes }))).toContain(marker);
});

it.each(outputFlags)("masks native and context semantic output copies with %s", (flag) => {
  for (const placement of ["native", "context"]) {
    const copies = { "output.value": marker, "raw.output": marker,
      "gen_ai.output.messages": JSON.stringify([{ role: "assistant", content: marker }]) };
    const result = mapGenkitAttributes({ ...flowAttributes(), ...(placement === "native" ? copies : {}) }, {
      policy: { ...SHOW_ALL, [flag]: true }, contextAttributes: placement === "context" ? copies : {},
    });
    expect(JSON.stringify(result)).not.toContain(marker);
    expect(result["output.value"]).toBe("__REDACTED__");
  }
});

it.each([...inputFlags, ...outputFlags, "hideErrorDetails", "hideEmbeddingVectors"] as const)(
  "masks opaque operation metadata with %s without changing the application copy", (flag) => {
    const operation = { id: "public-op", action: "/background-model/offline/model", done: true,
      metadata: { originalRequest: { messages: [{ role: "user", content: [{ text: marker }] }] }, arbitrary: marker },
      output: { message: { role: "model", content: [{ text: "public reply" }] }, finishReason: "stop" } };
    const original = JSON.stringify(operation);
    const sources = [
      { ...modelAttributes(), "genkit:metadata:subtype": "background-model", "genkit:output": original },
      { ...generateAttributes(), "genkit:output": JSON.stringify({ operation }) },
      ...["check-operation", "cancel-operation"].map((subtype) => ({ "genkit:type": "action", "genkit:metadata:subtype": subtype,
        "genkit:input": original, "genkit:output": original })),
    ];
    for (const source of sources) {
      const result = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, [flag]: true } });
      expect(JSON.stringify(result)).not.toContain(marker);
      expect(JSON.stringify(result)).toContain("__REDACTED__");
      expect(JSON.stringify(mapGenkitAttributes(source))).toContain(marker);
    }
    expect(JSON.stringify(operation)).toBe(original);
  },
);

it("retains opaque metadata when all content and error settings are explicitly off", () => {
  const operation = { id: "public-op", metadata: { arbitrary: marker } };
  const source = { "genkit:type": "action", "genkit:metadata:subtype": "check-operation", "genkit:output": JSON.stringify(operation) };
  expect(JSON.parse(mapGenkitAttributes(source, { policy: { ...SHOW_ALL, hideErrorDetails: false } })["genkit:output"] as string)).toEqual(operation);
});
