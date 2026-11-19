import { integrations } from "../src/libraries";

test("the TypeSafe AI SDK is registered for zero-code instrumentation with an import hook", () => {
  const row = integrations.find(item => item.name === "typesafe");
  expect(row).toEqual({
    name: "typesafe", package: "@traceai/typesafe", targets: ["@typesafe-ai/sdk"],
    className: "TypeSafeInstrumentation", modules: ["@typesafe-ai/sdk"],
  });
});
