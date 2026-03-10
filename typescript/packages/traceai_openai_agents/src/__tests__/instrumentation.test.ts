/**
 * Tests for OpenAI Agents instrumentation.
 */

import { OpenAIAgentsInstrumentation, FITracingProcessor, isPatched } from "../instrumentation";
import { VERSION } from "../version";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

describe("OpenAIAgentsInstrumentation", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new OpenAIAgentsInstrumentation();
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

  describe("OpenAIAgentsInstrumentation", () => {
    it("should create instrumentation instance", () => {
      const inst = new OpenAIAgentsInstrumentation();
      expect(inst).toBeInstanceOf(OpenAIAgentsInstrumentation);
    });

    it("should accept configuration options", () => {
      const inst = new OpenAIAgentsInstrumentation({
        traceConfig: {
          hideInputs: true,
          hideOutputs: true,
        },
      });
      expect(inst).toBeInstanceOf(OpenAIAgentsInstrumentation);
    });

    it("should provide getProcessor method", () => {
      instrumentation.enable();
      const processor = instrumentation.getProcessor();
      expect(processor).toBeInstanceOf(FITracingProcessor);
    });
  });

  describe("isPatched", () => {
    it("should return false when not patched", () => {
      instrumentation.disable();
      expect(isPatched()).toBe(false);
    });
  });
});

describe("FITracingProcessor", () => {
  const memoryExporter = new InMemorySpanExporter();
  const tracerProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memoryExporter)]
  });
  tracerProvider.register();

  const instrumentation = new OpenAIAgentsInstrumentation();
  instrumentation.setTracerProvider(tracerProvider);
  instrumentation.enable();

  let processor: FITracingProcessor;

  beforeEach(() => {
    memoryExporter.reset();
    processor = instrumentation.getProcessor();
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("onTraceStart/onTraceEnd", () => {
    it("should create and end root span for trace", () => {
      const trace = { traceId: "test-trace-1", name: "Test Agent" };

      processor.onTraceStart(trace);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);
      expect(spans[0].name).toBe("Test Agent");
    });
  });

  describe("onSpanStart/onSpanEnd", () => {
    it("should create span for agent span data", () => {
      const trace = { traceId: "test-trace-2", name: "Agent Run" };
      processor.onTraceStart(trace);

      const agentSpan = {
        spanId: "span-1",
        traceId: "test-trace-2",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "agent",
          name: "Assistant",
        },
      };

      processor.onSpanStart(agentSpan);
      processor.onSpanEnd(agentSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      expect(spans[0].name).toBe("Assistant");
    });

    it("should create span for function span data", () => {
      const trace = { traceId: "test-trace-3", name: "Agent Run" };
      processor.onTraceStart(trace);

      const functionSpan = {
        spanId: "span-2",
        traceId: "test-trace-3",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "function",
          name: "get_weather",
          input: { location: "San Francisco" },
          output: { temperature: 72 },
        },
      };

      processor.onSpanStart(functionSpan);
      processor.onSpanEnd(functionSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      expect(spans[0].name).toBe("get_weather");
    });

    it("should create span for generation span data", () => {
      const trace = { traceId: "test-trace-4", name: "Agent Run" };
      processor.onTraceStart(trace);

      const generationSpan = {
        spanId: "span-3",
        traceId: "test-trace-4",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "generation",
          name: "LLM Call",
          model: "gpt-4",
          modelConfig: { temperature: 0.7 },
          input: [{ role: "user", content: "Hello" }],
          output: [{ role: "assistant", content: "Hi there!" }],
          usage: { inputTokens: 5, outputTokens: 3 },
        },
      };

      processor.onSpanStart(generationSpan);
      processor.onSpanEnd(generationSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      expect(spans[0].name).toBe("LLM Call");
    });

    it("should handle handoff spans", () => {
      const trace = { traceId: "test-trace-5", name: "Multi-Agent Run" };
      processor.onTraceStart(trace);

      const handoffSpan = {
        spanId: "span-4",
        traceId: "test-trace-5",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "handoff",
          fromAgent: "Assistant",
          toAgent: "Specialist",
        },
      };

      processor.onSpanStart(handoffSpan);
      processor.onSpanEnd(handoffSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      expect(spans[0].name).toBe("handoff to Specialist");
    });

    it("should handle response spans with input/output", () => {
      const trace = { traceId: "test-trace-6", name: "Agent Run" };
      processor.onTraceStart(trace);

      const responseSpan = {
        spanId: "span-5",
        traceId: "test-trace-6",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "response",
          input: "What is the weather?",
          response: {
            output: [
              {
                type: "message",
                content: [{ type: "output_text", text: "The weather is sunny." }],
              },
            ],
          },
        },
      };

      processor.onSpanStart(responseSpan);
      processor.onSpanEnd(responseSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
    });

    it("should set error status on span error", () => {
      const trace = { traceId: "test-trace-7", name: "Agent Run" };
      processor.onTraceStart(trace);

      const errorSpan = {
        spanId: "span-6",
        traceId: "test-trace-7",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "function",
          name: "failing_tool",
        },
        error: { message: "Tool failed", data: "Connection timeout" },
      };

      processor.onSpanStart(errorSpan);
      processor.onSpanEnd(errorSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      expect(spans[0].status.code).toBe(2); // SpanStatusCode.ERROR
    });
  });

  describe("shutdown", () => {
    it("should clean up internal state", () => {
      processor.shutdown();
      // Should not throw
      expect(true).toBe(true);
    });
  });
});
