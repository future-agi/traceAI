import { integrations } from "../src/libraries";

test("H1 option ii advertises Mastra manual tracing without an auto enable hook", () => {
  const mastra = integrations.find(item => item.name === "mastra");
  expect(mastra).toEqual({
    name: "mastra", package: "@traceai/mastra",
    targets: ["@mastra/core", "@mastra/observability", "@mastra/otel-exporter"],
    manual: "Mastra zero-code bridge follows after 11-11; use createFIObservability / createFIMastraExporter from @traceai/mastra",
    modules: [],
  });
  expect(mastra?.enable).toBeUndefined();
  expect(mastra?.className).toBeUndefined();
  expect(integrations.find(item => item.name === "openai")?.className).toBe("OpenAIInstrumentation");
});
