import { mapGenkitAttributes, resolveContentPolicy, type ContentPolicy } from "../index";
import { generateAttributes, modelAttributes, toolAttributes } from "./fixtures";
import type { Attributes } from "@opentelemetry/api";

const marker = "SYNTHETIC_PRIVATE_NATIVE_CONTENT";
const request = { messages: [{ role: "user", content: [{ text: marker }] }] };
const response = { request, message: { role: "model", content: [{ text: "public reply" }] } };
const registered = { "genkit:type": "action", "genkit:metadata:subtype": "util", "genkit:key": "/util/generate", "genkit:name": "generate" };
const operation = {
  id: "public-id", action: "/background-model/offline/model", done: true, metadata: { stage: "safe" },
  error: { message: marker, details: { secret: marker }, status: "INTERNAL", code: 13 },
  output: { ...response, finishMessage: marker, error: { message: marker, status: "INTERNAL" } },
};
function map(attributes: Attributes, input: unknown, output: unknown, policy: ContentPolicy) {
  const source = { ...attributes, "genkit:input": JSON.stringify(input), "genkit:output": JSON.stringify(output) };
  const original = JSON.stringify(source);
  const out = mapGenkitAttributes(source, { policy });
  expect(JSON.stringify(source)).toBe(original);
  expect(out["genkit:output"]).toEqual(out["output.value"]);
  return out;
}

it.each(["hideInputs", "hideInputMessages", "hideInputText"] as const)("registered generate respects %s", (flag) => {
  const out = map(registered, request, response, resolveContentPolicy({ [flag]: true }));
  expect(JSON.stringify(out)).not.toContain(marker);
  expect(JSON.stringify(out)).toContain("public reply");
});

it.each([
  toolAttributes(),
  { ...registered, "genkit:key": "/util/other", "genkit:name": "other" },
  { "genkit:type": "util", "genkit:name": "other" },
])("does not interpret arbitrary util/tool output as a generation response (%#)", (attributes) => {
  const out = map(attributes, request, response, resolveContentPolicy({ hideInputText: true }));
  expect(JSON.parse(out["genkit:output"] as string)).toEqual(response);
});

it.each(["background-model", "check-operation", "cancel-operation"])("sanitizes %s Operation representations", (subtype) => {
  const attributes = { "genkit:type": "action", "genkit:metadata:subtype": subtype };
  const out = map(attributes, subtype === "background-model" ? request : operation, operation, resolveContentPolicy({ hideErrorDetails: true, hideInputText: true }));
  expect(JSON.stringify(out)).not.toContain(marker);
  const result = JSON.parse(out["genkit:output"] as string);
  expect(result).toMatchObject({ id: operation.id, action: operation.action, done: true, metadata: "__REDACTED__" });
  expect(result.error).toEqual({ status: "INTERNAL", code: 13 });
  expect(result.output.message).toEqual(response.message);
});

it("sanitizes nested generate operation without mutating the application object", () => {
  const original = JSON.stringify(operation);
  const out = map(generateAttributes(), request, { operation }, resolveContentPolicy({ hideErrorDetails: true, hideInputs: true }));
  expect(JSON.stringify(out)).not.toContain(marker);
  expect(JSON.stringify(operation)).toBe(original);
  expect(JSON.parse(out["genkit:output"] as string).operation.error).toEqual({ status: "INTERNAL", code: 13 });
});

it("preserves operation errors when explicitly opted out", () => {
  const out = map(generateAttributes(), {}, { operation }, resolveContentPolicy({ hideInputText: true, hideErrorDetails: false }));
  expect(JSON.parse(out["genkit:output"] as string).operation.error).toEqual(operation.error);
});

it("does not interpret an arbitrary non-model operation output as a generation response", () => {
  const value = { ...operation, action: "/custom/offline/job" };
  const out = map({ "genkit:type": "action", "genkit:metadata:subtype": "check-operation" }, value, value, resolveContentPolicy({ hideErrorDetails: true }));
  const result = JSON.parse(out["genkit:output"] as string);
  expect(result.error).toEqual({ status: "INTERNAL", code: 13 });
  expect(result.output).toEqual(operation.output);
});

it.each([generateAttributes(), modelAttributes(), registered])("image policy covers request and embedded request copies (%#)", (attributes) => {
  for (const media of [
    { url: `https://example.invalid/${marker}.png`, contentType: "image/png" },
    { url: `https://example.invalid/${marker}` },
    { url: `data:image/png;base64,${marker}` },
  ]) {
    const input = { messages: [{ role: "user", content: [{ text: "public text" }, { media }, { media: { url: "https://example.invalid/public.wav", contentType: "audio/wav" } }] }] };
    const out = map(attributes, input, { ...response, request: input }, resolveContentPolicy({ hideInputImages: true }));
    expect(JSON.stringify(out)).not.toContain(marker);
    expect(JSON.stringify(out)).toContain("public text");
    expect(JSON.stringify(out)).toContain("public reply");
    expect(JSON.stringify(out)).toContain("public.wav");
    expect(input.messages[0].content[1].media).toEqual(media);
  }
});

it("resolves image flag precedence without implying error hiding", () => {
  const previous = process.env.FI_HIDE_INPUT_IMAGES;
  try {
    process.env.FI_HIDE_INPUT_IMAGES = "true";
    const imageRequest = { messages: [{ content: [{ media: { url: marker, contentType: "image/png" } }] }] };
    for (const [options, hidden] of [[{}, true], [{ hideInputImages: false }, false]] as const) {
      const policy = resolveContentPolicy(options);
      expect(policy.hideErrorDetails).toBe(false);
      const out = map(registered, imageRequest, { request: imageRequest }, policy);
      expect(JSON.stringify(out).includes(marker)).toBe(!hidden);
    }
  } finally {
    if (previous === undefined) delete process.env.FI_HIDE_INPUT_IMAGES;
    else process.env.FI_HIDE_INPUT_IMAGES = previous;
  }
});

it("fails closed on truncated Operation and image-request JSON", () => {
  for (const [attributes, policy] of [
    [{ "genkit:type": "action", "genkit:metadata:subtype": "check-operation" }, resolveContentPolicy({ hideErrorDetails: true })],
    [registered, resolveContentPolicy({ hideInputImages: true })],
  ] as const) {
    const out = mapGenkitAttributes({ ...attributes, "genkit:input": '{"private":', "genkit:output": '{"private":' }, { policy });
    expect(out["genkit:input"]).toBe("__REDACTED__");
    expect(out["genkit:output"]).toBe("__REDACTED__");
  }
});
