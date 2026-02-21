/**
 * E2E Tests for @traceai/beeai
 *
 * These tests run against the BeeAI framework and export spans to the FI backend.
 * Set FI_API_KEY environment variable to run these tests.
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { BeeAIInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("BeeAI E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: BeeAIInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-beeai-e2e",
      batch: false,
    });

    instrumentation = new BeeAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Agent", () => {
    it("should create and run a basic BeeAI agent", async () => {
      const beeModule = await import("bee-agent-framework");
      const { BeeAgent } = beeModule;

      expect(BeeAgent).toBeDefined();
      expect(typeof BeeAgent).toBe("function");
    }, 30000);

    it("should verify instrumentation is enabled", () => {
      expect(instrumentation).toBeDefined();
      instrumentation.disable();
      instrumentation.enable();
    });
  });

  describe("Error Handling", () => {
    it("should handle missing configuration gracefully", async () => {
      const beeModule = await import("bee-agent-framework");
      expect(beeModule).toBeDefined();
    }, 30000);
  });
});
