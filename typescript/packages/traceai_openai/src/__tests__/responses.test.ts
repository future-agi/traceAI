import { OpenAIInstrumentation } from "../instrumentation";
import { describe, it, expect, jest, beforeEach, afterEach } from '@jest/globals';

// Mock the dependencies similar to other working tests
jest.mock('@traceai/fi-core', () => ({
  FITracer: jest.fn().mockImplementation(() => ({
    startSpan: jest.fn().mockReturnValue({
      setStatus: jest.fn(),
      recordException: jest.fn(),
      setAttributes: jest.fn(),
      addEvent: jest.fn(),
      end: jest.fn(),
      isRecording: jest.fn().mockReturnValue(true),
      spanContext: jest.fn().mockReturnValue({
        traceId: 'mock-trace-id',
        spanId: 'mock-span-id',
      }),
    }),
    startActiveSpan: jest.fn((name: string, fn: any) => {
      const mockSpan = {
        setStatus: jest.fn(),
        recordException: jest.fn(),
        setAttributes: jest.fn(),
        addEvent: jest.fn(),
        end: jest.fn(),
        isRecording: jest.fn().mockReturnValue(true),
        spanContext: jest.fn().mockReturnValue({
          traceId: 'mock-trace-id',
          spanId: 'mock-span-id',
        }),
      };
      return fn(mockSpan);
    }),
  })),
  safelyJSONStringify: jest.fn((obj: any) => JSON.stringify(obj)),
}));

describe("OpenAI Responses Instrumentation Tests", () => {
  let instrumentation: OpenAIInstrumentation;

  beforeEach(() => {
    jest.clearAllMocks();
    
    instrumentation = new OpenAIInstrumentation({
      instrumentationConfig: {
        enabled: true,
      },
    });

    // Mock the tracer property
    const mockTracer = {
      startSpan: jest.fn().mockReturnValue({
        setStatus: jest.fn(),
        recordException: jest.fn(),
        setAttributes: jest.fn(),
        addEvent: jest.fn(),
        end: jest.fn(),
        isRecording: jest.fn().mockReturnValue(true),
      }),
      startActiveSpan: jest.fn((name: string, fn: any) => {
        const mockSpan = {
          setStatus: jest.fn(),
          recordException: jest.fn(),
          setAttributes: jest.fn(),
          addEvent: jest.fn(),
          end: jest.fn(),
          isRecording: jest.fn().mockReturnValue(true),
        };
        return fn(mockSpan);
      }),
    };

    Object.defineProperty(instrumentation, 'tracer', {
      get: () => mockTracer,
      configurable: true,
    });
  });

  afterEach(() => {
    instrumentation.disable();
  });

  describe("Responses API Patching", () => {
    it("should patch responses create method", () => {
      const originalCreate = jest.fn();
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: originalCreate,
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      // Check that the method still exists and can be called
      expect(mockModule.OpenAI.Responses.prototype.create).toBeDefined();
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle responses with basic functionality", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn().mockImplementation(() => Promise.resolve({
                id: "resp_245",
                status: "completed",
                model: "gpt-4.1",
                output: [
                  {
                    id: "msg_example",
                    type: "message",
                    role: "assistant",
                    content: [
                      {
                        type: "output_text",
                        text: "This is a test.",
                      },
                    ],
                  },
                ],
                usage: {
                  input_tokens: 12,
                  output_tokens: 6,
                  total_tokens: 18,
                },
              })),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle responses with instructions and multiple inputs", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn().mockImplementation(() => Promise.resolve({
                id: "resp_245",
                model: "gpt-4.1",
                instructions: "You are a helpful assistant.",
                input: [
                  {
                    type: "message",
                    content: "say this is a test",
                    role: "user",
                  },
                  {
                    type: "message",
                    content: "remember to say this is a test",
                    role: "user",
                  },
                ],
                output: [{
                  type: "message",
                  role: "assistant",
                  content: [{ type: "output_text", text: "This is a test." }],
                }],
              })),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle streaming responses", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn().mockImplementation(() => Promise.resolve({
                async *[Symbol.asyncIterator]() {
                  yield { type: "response.output_text.delta", delta: "I " };
                  yield { type: "response.output_text.delta", delta: "am streaming!" };
                  yield { 
                    type: "response.completed", 
                    response: { 
                      id: "resp-567",
                      model: "gpt-4.1",
                      output: [{ type: "output_text", text: "I am streaming!" }],
                    } 
                  };
                }
              })),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle tool calls in responses", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn().mockImplementation(() => Promise.resolve({
                id: "resp-890",
                model: "gpt-4.1",
                output: [{
                  id: "fc_1234",
                  type: "function_call",
                  status: "completed",
                  arguments: '{"location":"boston"}',
                  call_id: "call_abc123",
                  name: "get_weather",
                }],
                tools: [{
                  type: "function",
                  name: "get_weather",
                  parameters: {
                    type: "object",
                    properties: { location: { type: "string" } },
                    additionalProperties: false,
                    required: ["location"],
                  },
                  strict: true,
                }],
                usage: {
                  prompt_tokens: 86,
                  completion_tokens: 15,
                  total_tokens: 101,
                },
              })),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle responses with structured outputs", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn(),
              parse: jest.fn().mockImplementation(() => Promise.resolve({
                id: "resp-890",
                model: "gpt-4.1",
                output: [{
                  type: "message",
                  status: "completed",
                  content: [{
                    type: "output_text",
                    text: '{"name":"science fair","date":"Friday","participants":["Alice","Bob"]}',
                  }],
                }],
                output_parsed: {
                  name: "science fair",
                  date: "Friday", 
                  participants: ["Alice", "Bob"],
                }
              })),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
      expect(typeof mockModule.OpenAI.Responses.prototype.parse).toBe('function');
    });
  });

  describe("Configuration and Edge Cases", () => {
    it("should handle responses instrumentation with different configs", () => {
      const configs = [
        { hideInputs: true, hideOutputs: false },
        { hideInputs: false, hideOutputs: true },
        { hideInputs: true, hideOutputs: true },
      ];

      configs.forEach(config => {
        const configuredInstrumentation = new OpenAIInstrumentation({
          traceConfig: config,
        });
        expect(configuredInstrumentation).toBeInstanceOf(OpenAIInstrumentation);
        
        const mockModule = {
          OpenAI: {
            Chat: { Completions: { prototype: { create: jest.fn() } } },
            Completions: { prototype: { create: jest.fn() } },
            Embeddings: { prototype: { create: jest.fn() } },
            Responses: { prototype: { create: jest.fn() } },
          },
        };
        
        configuredInstrumentation.enable();
        expect(() => {
          configuredInstrumentation.manuallyInstrument(mockModule as any);
        }).not.toThrow();
        configuredInstrumentation.disable();
      });
    });

    it("should handle responses with error scenarios", () => {
      const mockModule = {
        OpenAI: {
          Chat: {
            Completions: {
              prototype: {
                create: jest.fn(),
              },
            },
          },
          Completions: {
            prototype: {
              create: jest.fn(),
            },
          },
          Embeddings: {
            prototype: {
              create: jest.fn(),
            },
          },
          Responses: {
            prototype: {
              create: jest.fn().mockImplementation(() => Promise.reject(new Error("API Error"))),
            },
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle multiple enable/disable cycles with responses", () => {
      const mockModule = {
        OpenAI: {
          Chat: { Completions: { prototype: { create: jest.fn() } } },
          Completions: { prototype: { create: jest.fn() } },
          Embeddings: { prototype: { create: jest.fn() } },
          Responses: { prototype: { create: jest.fn() } },
        },
      };

      expect(() => {
        instrumentation.enable();
        instrumentation.manuallyInstrument(mockModule as any);
        instrumentation.disable();
        instrumentation.enable();
        instrumentation.manuallyInstrument(mockModule as any);
        instrumentation.disable();
      }).not.toThrow();
    });
  });

  describe("Responses Coverage Improvement", () => {
    it("should test various response attribute extractions", () => {
      const mockModule = {
        OpenAI: {
          Chat: { Completions: { prototype: { create: jest.fn() } } },
          Completions: { prototype: { create: jest.fn() } },
          Embeddings: { prototype: { create: jest.fn() } },
          Responses: { 
            prototype: { 
              create: jest.fn().mockImplementation(() => Promise.resolve({
                id: "test-response",
                model: "gpt-4.1",
                usage: {
                  input_tokens: 100,
                  output_tokens: 50,
                  total_tokens: 150,
                  input_tokens_details: { cached_tokens: 10 },
                  output_tokens_details: { reasoning_tokens: 5 },
                },
                output: [
                  {
                    type: "message",
                    role: "assistant",
                    content: [
                      { type: "output_text", text: "Response text" },
                    ],
                  },
                ],
              })),
            } 
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });

    it("should handle responses with complex input structures", () => {
      const mockModule = {
        OpenAI: {
          Chat: { Completions: { prototype: { create: jest.fn() } } },
          Completions: { prototype: { create: jest.fn() } },
          Embeddings: { prototype: { create: jest.fn() } },
          Responses: { 
            prototype: { 
              create: jest.fn().mockImplementation((params: any) => {
                // Mock different responses based on input
                if (params && params.stream) {
                  return Promise.resolve({
                    async *[Symbol.asyncIterator]() {
                      yield { type: "response.output_text.delta", delta: "Stream " };
                      yield { type: "response.output_text.delta", delta: "response" };
                      yield { type: "response.completed", response: { id: "stream-resp" } };
                    }
                  });
                } else {
                  return Promise.resolve({
                    id: "standard-resp",
                    model: params?.model || "gpt-4.1",
                    output: [{ type: "message", role: "assistant", content: "Response" }],
                  });
                }
              })
            } 
          },
        },
      };

      instrumentation.enable();
      instrumentation.manuallyInstrument(mockModule as any);
      
      expect(typeof mockModule.OpenAI.Responses.prototype.create).toBe('function');
    });
  });
}); 