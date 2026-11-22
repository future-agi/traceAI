import { integrations } from "../src/libraries";

test("the SageMaker Runtime client is registered for zero-code instrumentation with an import hook", () => {
  const row = integrations.find(item => item.name === "sagemaker");
  expect(row).toEqual({
    name: "sagemaker", package: "@traceai/sagemaker", targets: ["@aws-sdk/client-sagemaker-runtime"],
    className: "SageMakerInstrumentation", modules: ["@aws-sdk/client-sagemaker-runtime"],
  });
});
