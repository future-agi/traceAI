/**
 * Tests for OpenAI Agents instrumentation.
 */

import {
  OpenAIAgentsInstrumentation,
  FITracingProcessor,
  isPatched,
  SUPPORTED_AGENTS_VERSIONS,
  _resetPatchedStateForTesting,
} from "../instrumentation";
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
      expect(VERSION).toBe("0.2.0");
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

  describe("processor registration", () => {
    function fakeAgentsModule() {
      return {
        setTraceProcessors: jest.fn(),
        addTraceProcessor: jest.fn(),
      } as unknown as Parameters<OpenAIAgentsInstrumentation["manuallyInstrument"]>[0] & {
        setTraceProcessors: jest.Mock;
        addTraceProcessor: jest.Mock;
      };
    }

    afterEach(() => {
      _resetPatchedStateForTesting();
    });

    it("replaces the SDK's trace processors by default", () => {
      _resetPatchedStateForTesting();
      const inst = new OpenAIAgentsInstrumentation();
      inst.setTracerProvider(tracerProvider);
      const agentsModule = fakeAgentsModule();
      inst.manuallyInstrument(agentsModule);
      expect(agentsModule.setTraceProcessors).toHaveBeenCalledTimes(1);
      expect(agentsModule.addTraceProcessor).not.toHaveBeenCalled();
      inst.disable();
    });

    it("keeps the SDK's default exporter when exclusiveProcessor is false", () => {
      _resetPatchedStateForTesting();
      const inst = new OpenAIAgentsInstrumentation({ exclusiveProcessor: false });
      inst.setTracerProvider(tracerProvider);
      const agentsModule = fakeAgentsModule();
      inst.manuallyInstrument(agentsModule);
      expect(agentsModule.addTraceProcessor).toHaveBeenCalledTimes(1);
      expect(agentsModule.setTraceProcessors).not.toHaveBeenCalled();
      inst.disable();
    });

    it("accepts a non-extensible module object such as an ES module namespace", () => {
      _resetPatchedStateForTesting();
      const inst = new OpenAIAgentsInstrumentation();
      inst.setTracerProvider(tracerProvider);
      const agentsModule = Object.preventExtensions(fakeAgentsModule());
      expect(() => inst.manuallyInstrument(agentsModule)).not.toThrow();
      expect(agentsModule.setTraceProcessors).toHaveBeenCalledTimes(1);
      expect(isPatched()).toBe(true);
      inst.disable();
    });

    it("declares the tested @openai/agents range for the require hook", () => {
      expect(SUPPORTED_AGENTS_VERSIONS).toEqual([">=0.4.0 <0.22.0"]);
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
          model_config: { temperature: 0.7 },
          input: [{ role: "user", content: "Hello" }],
          output: [{ role: "assistant", content: "Hi there!" }],
          usage: { input_tokens: 5, output_tokens: 3 },
        },
      };

      processor.onSpanStart(generationSpan);
      processor.onSpanEnd(generationSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      expect(spans[0].name).toBe("LLM Call");
      expect(spans[0].attributes["gen_ai.request.model"]).toBe("gpt-4");
      expect(spans[0].attributes["gen_ai.request.parameters"]).toBe(
        JSON.stringify({ temperature: 0.7 })
      );
      expect(spans[0].attributes["gen_ai.usage.input_tokens"]).toBe(5);
      expect(spans[0].attributes["gen_ai.usage.output_tokens"]).toBe(3);
      expect(spans[0].attributes["gen_ai.usage.total_tokens"]).toBe(8);
    });

    it("should read token counts from a chat completion when usage is absent", () => {
      const trace = { traceId: "test-trace-4b", name: "Agent Run" };
      processor.onTraceStart(trace);

      const generationSpan = {
        spanId: "span-3b",
        traceId: "test-trace-4b",
        startedAt: new Date().toISOString(),
        spanData: {
          type: "generation",
          model: "gpt-4.1-mini",
          input: [{ role: "user", content: "Hello" }],
          output: [
            {
              object: "chat.completion",
              choices: [{ message: { role: "assistant", content: "Hi there!" } }],
              usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 },
            },
          ],
        },
      };

      processor.onSpanStart(generationSpan);
      processor.onSpanEnd(generationSpan);
      processor.onTraceEnd(trace);

      const [generation, root] = memoryExporter.getFinishedSpans();
      expect(generation.attributes["output.value"]).toBe("Hi there!");
      expect(generation.attributes["gen_ai.usage.input_tokens"]).toBe(4);
      expect(generation.attributes["gen_ai.usage.output_tokens"]).toBe(2);
      expect(generation.attributes["gen_ai.usage.total_tokens"]).toBe(6);
      expect(root.attributes["output.value"]).toBe("Hi there!");
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
          from_agent: "Assistant",
          to_agent: "Specialist",
        },
      };
      const specialistSpan = {
        spanId: "span-4b",
        traceId: "test-trace-5",
        startedAt: new Date().toISOString(),
        spanData: { type: "agent", name: "Specialist" },
      };

      processor.onSpanStart(handoffSpan);
      processor.onSpanEnd(handoffSpan);
      processor.onSpanStart(specialistSpan);
      processor.onSpanEnd(specialistSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(3);
      expect(spans[0].name).toBe("handoff to Specialist");
      expect(spans[1].attributes["graph.node.id"]).toBe("Specialist");
      expect(spans[1].attributes["graph.node.parent_id"]).toBe("Assistant");
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
          response_id: "resp_1",
          _input: "What is the weather?",
          _response: {
            id: "resp_1",
            model: "gpt-4.1-mini",
            output: [
              {
                type: "message",
                content: [{ type: "output_text", text: "The weather is sunny." }],
              },
            ],
            usage: { input_tokens: 9, output_tokens: 5, total_tokens: 14 },
          },
        },
      };

      processor.onSpanStart(responseSpan);
      processor.onSpanEnd(responseSpan);
      processor.onTraceEnd(trace);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(2);
      const [response, root] = spans;
      expect(response.attributes["input.value"]).toBe("What is the weather?");
      expect(response.attributes["output.value"]).toBe("The weather is sunny.");
      expect(response.attributes["gen_ai.request.model"]).toBe("gpt-4.1-mini");
      expect(response.attributes["gen_ai.usage.input_tokens"]).toBe(9);
      expect(response.attributes["gen_ai.usage.output_tokens"]).toBe(5);
      expect(root.attributes["input.value"]).toBe("What is the weather?");
      expect(root.attributes["output.value"]).toBe("The weather is sunny.");
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
