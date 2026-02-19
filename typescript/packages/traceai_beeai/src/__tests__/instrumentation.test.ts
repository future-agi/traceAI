/**
 * Tests for BeeAI instrumentation.
 */

import { BeeAIInstrumentation, isPatched } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("BeeAIInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new BeeAIInstrumentation();
  instrumentation.disable();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    memoryExporter.reset();
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("VERSION", () => {
    it("should export VERSION constant", () => {
      expect(VERSION).toBe("0.1.0");
    });
  });

  describe("BeeAIInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new BeeAIInstrumentation();
      expect(inst).toBeInstanceOf(BeeAIInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new BeeAIInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(BeeAIInstrumentation);
    });
  });

  describe("isPatched", () => {
    it("should return false when not patched", () => {
      instrumentation.disable();
      expect(isPatched()).toBe(false);
    });
  });

  describe("manuallyInstrument", () => {
    it("should handle manual instrumentation of mock module", () => {
      instrumentation.enable();

      // Create a mock BeeAgent class
      const mockBeeModule = {
        BeeAgent: class {
          async run(input: string) {
            return { output: `Processed: ${input}` };
          }
        },
      };

      // Should not throw
      instrumentation.manuallyInstrument(mockBeeModule);
      expect(isPatched()).toBe(true);
    });
  });
});

describe("BeeAI Agent Tracing", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new BeeAIInstrumentation();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    memoryExporter.reset();
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation.disable();
  });

  it("should trace agent run with mock", async () => {
    // Create a mock BeeAgent class with instrumented behavior
    class MockBeeAgent {
      async run(input: string) {
        return { output: `Response to: ${input}` };
      }
    }

    const mockModule = { BeeAgent: MockBeeAgent };
    instrumentation.manuallyInstrument(mockModule);

    const agent = new mockModule.BeeAgent();
    const result = await agent.run("Hello");

    expect(result).toEqual({ output: "Response to: Hello" });

    // Check spans were created
    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBeGreaterThanOrEqual(1);
  });
});
