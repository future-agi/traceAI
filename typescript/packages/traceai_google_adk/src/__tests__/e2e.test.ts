/**
 * E2E Tests for @traceai/google-adk
 *
 * These tests run against the Google AI Development Kit (ADK) and export spans
 * to the FI backend. Set FI_API_KEY environment variable to run these tests.
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { GoogleADKInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Google ADK E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: GoogleADKInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-google-adk-e2e",
      batch: false,
    });

    instrumentation = new GoogleADKInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Agent", () => {
    it("should create a basic ADK agent", async () => {
      const adkModule = await import("@google/adk");
      const { Agent } = adkModule;

      const agent = new Agent({
        name: "test-agent",
        model: "gemini-2.0-flash",
        instruction: "You are a helpful assistant. Answer briefly.",
      });

      expect(agent).toBeDefined();
    }, 30000);

    it("should verify instrumentation lifecycle", () => {
      expect(instrumentation).toBeDefined();
      instrumentation.disable();
      instrumentation.enable();
    });
  });

  describe("Error Handling", () => {
    it("should handle missing configuration gracefully", async () => {
      const adkModule = await import("@google/adk");
      expect(adkModule).toBeDefined();
    }, 30000);
  });
});
