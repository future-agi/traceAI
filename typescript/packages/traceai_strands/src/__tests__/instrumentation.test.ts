/**
 * Tests for Strands instrumentation.
 */

import { StrandsInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("StrandsInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new StrandsInstrumentation();
  instrumentation.disable();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    _resetPatchedStateForTesting();
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

  describe("StrandsInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new StrandsInstrumentation();
      expect(inst).toBeInstanceOf(StrandsInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new StrandsInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(StrandsInstrumentation);
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

      // Create a mock Agent class
      const mockStrandsModule = {
        Agent: class {
          name = "TestAgent";
          model = "us.anthropic.claude-sonnet-4-20250514-v1:0";

          async invoke(input: string) {
            return { text: `Response to: ${input}` };
          }
        },
      };

      // Should not throw
      instrumentation.manuallyInstrument(mockStrandsModule);
      expect(isPatched()).toBe(true);
    });
  });
});

describe("Strands Agent Tracing", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new StrandsInstrumentation();
  instrumentation.setTracerProvider(tracerProvider);

  beforeEach(() => {
    _resetPatchedStateForTesting();
    memoryExporter.reset();
    instrumentation.enable();
  });

  afterEach(() => {
    instrumentation.disable();
  });

  it("should trace agent invoke with mock", async () => {
    // Create a mock Agent class with instrumented behavior
    class MockAgent {
      name = "Assistant";
      model = "us.anthropic.claude-sonnet-4-20250514-v1:0";

      async invoke(input: string) {
        return {
          text: `Response to: ${input}`,
          usage: {
            inputTokens: 10,
            outputTokens: 20,
            totalTokens: 30,
          },
        };
      }
    }

    const mockModule = { Agent: MockAgent };
    instrumentation.manuallyInstrument(mockModule);

    const agent = new mockModule.Agent();
    const result = await agent.invoke("Hello");

    expect(result.text).toBe("Response to: Hello");

    // Check spans were created
    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBeGreaterThanOrEqual(1);
  });

  it("should capture model provider from model name", async () => {
    class MockAgent {
      name = "ClaudeAgent";
      model = "us.anthropic.claude-sonnet-4-20250514-v1:0";

      async invoke(input: string) {
        return { text: `Response: ${input}` };
      }
    }

    const mockModule = { Agent: MockAgent };
    instrumentation.manuallyInstrument(mockModule);

    const agent = new mockModule.Agent();
    await agent.invoke("Hello");

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBeGreaterThanOrEqual(1);
    // Model name should be captured
    const span = spans[0];
    expect(span.attributes["gen_ai.request.model"]).toBe("us.anthropic.claude-sonnet-4-20250514-v1:0");
  });

  it("should handle errors gracefully", async () => {
    class MockAgent {
      name = "FailingAgent";
      model = "test-model";

      async invoke(_input: string) {
        throw new Error("Agent failed");
      }
    }

    const mockModule = { Agent: MockAgent };
    instrumentation.manuallyInstrument(mockModule);

    const agent = new mockModule.Agent();

    await expect(agent.invoke("Hello")).rejects.toThrow("Agent failed");

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBeGreaterThanOrEqual(1);
    expect(spans[0].status.code).toBe(2); // SpanStatusCode.ERROR
  });
});
