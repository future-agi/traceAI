import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { generateTraceConfig, REDACTED_VALUE } from "@traceai/fi-core";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { sanitizeSpan } from "../privacy";

function converted(attributes: Record<string, unknown> = {}): ReadableSpan {
  return {
    attributes,
    status: { code: 2, message: "PRIVATE_ERROR\n    at run (/tmp/test.js:1:2)" },
    events: [{ name: "exception", time: [0, 0], attributes: {
      "exception.type": "Error", "exception.message": "PRIVATE_ERROR",
      "exception.stacktrace": "PRIVATE_STACK", "exception.escaped": true,
      "mastra.tool.error_message": "PRIVATE_ERROR", "custom.number": 1,
    } }],
  } as unknown as ReadableSpan;
}

describe("Mastra export privacy", () => {
  it("applies content policy to both span and event aliases without mutation", () => {
    const attributes = Object.fromEntries([
      "input.value", "raw.input", "gen_ai.input.messages", "gen_ai.prompt",
      SemanticConventions.PROMPT_TEMPLATE_VARIABLES, "gen_ai.system_instructions",
      "gen_ai.tool.call.arguments", "mastra.agent_run.input",
      "output.value", "raw.output", "gen_ai.output.messages", "gen_ai.completion",
      "gen_ai.tool.call.result", "mastra.workflow_run.output", "error.message",
    ].map(key => [key, "PRIVATE_CONTENT"]));
    attributes["gen_ai.usage.input_tokens"] = "11";
    const span = converted(attributes);
    span.events[0].attributes = { ...span.events[0].attributes, ...attributes };
    const original = JSON.stringify(span);
    const result = sanitizeSpan(span, generateTraceConfig({ hideInputs: true, hideOutputs: true, hideErrorDetails: true }));
    expect(JSON.stringify(result)).not.toContain("PRIVATE_");
    expect(result.attributes["gen_ai.usage.input_tokens"]).toBe("11");
    expect(result.status).toEqual({ code: 2 });
    expect(result.events[0].attributes).toMatchObject({ "exception.type": "Error", "exception.escaped": true, "custom.number": 1 });
    expect(JSON.stringify(span)).toBe(original);
  });

  it("masks credentials in nested serialized data, flattened metadata and events", () => {
    const span = converted({
      "input.value": JSON.stringify({ nested: [{ apiKey: "PRIVATE_KEY", accessToken: "PRIVATE_TOKEN", q: "keep" }] }),
      "mastra.metadata.api_key": "PRIVATE_KEY",
      "mastra.metadata.headers": JSON.stringify({ Authorization: "Bearer PRIVATE_AUTH", "x-api-key": "PRIVATE_KEY" }),
    });
    span.events[0].attributes = { "debug": "apiKey=PRIVATE_KEY", "x-secret-key": "PRIVATE_SECRET" };
    const result = sanitizeSpan(span, generateTraceConfig({ hideErrorDetails: true }));
    expect(JSON.stringify(result)).not.toContain("PRIVATE_");
    expect(result.attributes["input.value"]).toContain("keep");
    expect(result.attributes["mastra.metadata.api_key"]).toBe(REDACTED_VALUE);
  });

  it("hides image-containing aliases independently of text flags", () => {
    const span = converted({ "input.value": "PRIVATE_IMAGE", "mastra.tool_call.input": "PRIVATE_IMAGE", "gen_ai.input.messages": "PRIVATE_IMAGE" });
    const result = sanitizeSpan(span, generateTraceConfig({ hideInputImages: true }));
    expect(JSON.stringify(result.attributes)).not.toContain("PRIVATE_IMAGE");
  });

  it("hides vectors from both native and FI embedding output", () => {
    const result = sanitizeSpan(converted({
      "mastra.span.type": "rag_embedding", "embedding.embeddings.0.vector": [0.1234567],
      "output.value": "[[0.1234567]]", "mastra.rag_embedding.output": "[[0.1234567]]",
      "gen_ai.usage.input_tokens": 5,
    }), generateTraceConfig({ hideEmbeddingVectors: true }));
    expect(JSON.stringify(result.attributes)).not.toContain("0.1234567");
    expect(result.attributes["gen_ai.usage.input_tokens"]).toBe(5);
  });

  it("keeps error messages without status stacks when explicitly enabled", () => {
    const result = sanitizeSpan(converted(), generateTraceConfig({ hideInputs: true, hideErrorDetails: false }));
    expect(result.status).toEqual({ code: 2, message: "PRIVATE_ERROR" });
    expect(result.events[0].attributes?.["exception.message"]).toBe("PRIVATE_ERROR");
  });
});
