import { integrations } from "../src/libraries";

test("packages added in the 2026-11-09 train are registered with their real setup mode", () => {
  expect(integrations.find(item => item.name === "n8n")).toEqual({
    name: "n8n", package: "@traceai/n8n", targets: ["n8n-core"], className: "N8nInstrumentation", modules: ["n8n-core"],
  });
  for (const [name, pkg, target] of [["openrouter", "@traceai/openrouter", "@openrouter/sdk"], ["vitest-evals", "@traceai/vitest-evals", "vitest-evals"]]) {
    const row = integrations.find(item => item.name === name);
    expect(row).toMatchObject({ package: pkg, targets: [target], modules: [] });
    expect(row!.manual).toEqual(expect.any(String));
    expect(row!.className).toBeUndefined();
    expect(row!.enable).toBeUndefined();
  }
});
