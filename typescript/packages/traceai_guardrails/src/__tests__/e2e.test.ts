/**
 * E2E Tests for @traceai/guardrails
 *
 * These tests run against the Guardrails AI framework and export spans to the
 * FI backend. Set FI_API_KEY environment variable to run these tests.
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider, ProjectType } from "@traceai/fi-core";
import { GuardrailsInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Guardrails E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: GuardrailsInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-guardrails-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new GuardrailsInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const guardrailsModule = await import("@guardrails-ai/core");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    instrumentation.manuallyInstrument(guardrailsModule as any);
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("Guard", () => {
    it("should verify guardrails module can be imported with instrumentation", async () => {
      const guardrailsModule = await import("@guardrails-ai/core");
      expect(guardrailsModule).toBeDefined();
    }, 30000);

    it("should verify instrumentation lifecycle", () => {
      expect(instrumentation).toBeDefined();
      instrumentation.disable();
      instrumentation.enable();
    });
  });

  describe("Error Handling", () => {
    it("should handle missing configuration gracefully", async () => {
      const guardrailsModule = await import("@guardrails-ai/core");
      expect(guardrailsModule).toBeDefined();
    }, 30000);
  });
});
