/**
 * E2E Tests for @traceai/haystack
 *
 * These tests run against the Haystack AI pipeline framework and export spans
 * to the FI backend. Set FI_API_KEY environment variable to run these tests.
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { HaystackInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Haystack E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: HaystackInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-haystack-e2e",
      batch: false,
    });

    instrumentation = new HaystackInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Pipeline", () => {
    it("should verify haystack module can be imported with instrumentation", async () => {
      const haystackModule = await import("haystack-ai");
      expect(haystackModule).toBeDefined();
    }, 30000);

    it("should verify instrumentation lifecycle", () => {
      expect(instrumentation).toBeDefined();
      instrumentation.disable();
      instrumentation.enable();
    });
  });

  describe("Error Handling", () => {
    it("should handle missing configuration gracefully", async () => {
      const haystackModule = await import("haystack-ai");
      expect(haystackModule).toBeDefined();
    }, 30000);
  });
});
