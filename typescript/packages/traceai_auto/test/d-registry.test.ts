import { integrations } from "../src/libraries";

test("R-D-REGISTRY: newly staged packages advertise only their actual manual capabilities", () => {
  const expected = [
    { name: "elevenlabs", package: "@traceai/elevenlabs", className: "ElevenLabsInstrumentation", targets: ["@elevenlabs/elevenlabs-js"] },
    { name: "google-genai-sdk", package: "@traceai/google-genai-sdk", targets: ["@google/genai"] },
    { name: "huggingface-transformers", package: "@traceai/huggingface-transformers", className: "TransformersInstrumentation", targets: ["@huggingface/transformers"] },
  ];
  for (const row of expected) {
    const actual = integrations.find(item => item.package === row.package);
    expect(actual).toBeDefined();
    expect(actual).toMatchObject({ ...row, modules: [] });
    expect(actual!.manual).toEqual(expect.any(String));
    expect(actual!.enable).toBeUndefined();
  }
  expect(integrations.find(item => item.name === "google-genai-sdk")!.className).toBeUndefined();
});
