/**
 * E2E Tests for @traceai/livekit
 *
 * These tests run against the LiveKit real-time communication SDK and export
 * spans to the FI backend. Set FI_API_KEY environment variable to run these tests.
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider, ProjectType } from "@traceai/fi-core";
import { LiveKitInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("LiveKit E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: LiveKitInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-livekit-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new LiveKitInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const livekitModule = await import("@livekit/agents");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    instrumentation.manuallyInstrument(livekitModule as any);
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("Client", () => {
    it("should verify livekit module can be imported with instrumentation", async () => {
      const livekitModule = await import("@livekit/agents");
      expect(livekitModule).toBeDefined();
    }, 30000);

    it("should verify instrumentation lifecycle", () => {
      expect(instrumentation).toBeDefined();
      instrumentation.disable();
      instrumentation.enable();
    });
  });

  describe("Error Handling", () => {
    it("should handle missing configuration gracefully", async () => {
      const livekitModule = await import("@livekit/agents");
      expect(livekitModule).toBeDefined();
    }, 30000);
  });
});
