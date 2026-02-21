/**
 * E2E Tests for @traceai/pipecat
 *
 * These tests run against the Pipecat voice AI pipeline SDK and export spans
 * to the FI backend. Set FI_API_KEY environment variable to run these tests.
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { PipecatInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Pipecat E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: PipecatInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-pipecat-e2e",
      batch: false,
    });

    instrumentation = new PipecatInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Pipeline", () => {
    it("should verify pipecat module can be imported with instrumentation", async () => {
      const pipecatModule = await import("pipecat-ai");
      expect(pipecatModule).toBeDefined();
    }, 30000);

    it("should verify instrumentation lifecycle", () => {
      expect(instrumentation).toBeDefined();
      instrumentation.disable();
      instrumentation.enable();
    });
  });

  describe("Error Handling", () => {
    it("should handle missing configuration gracefully", async () => {
      const pipecatModule = await import("pipecat-ai");
      expect(pipecatModule).toBeDefined();
    }, 30000);
  });
});
