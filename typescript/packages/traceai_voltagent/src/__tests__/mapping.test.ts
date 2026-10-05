import { FISpanKind } from "@traceai/fi-semantic-conventions";
import {
  isContentKey,
  isPromotedUsageKey,
  isSecretKey,
  mapVoltAgentAttributes,
  reconcileOperationUsage,
  resolveSpanKind,
} from "../mapping";

const OFF = { captureContent: false };
const ON = { captureContent: true };

/** Keys trace-context.ts (2.11.0) writes on an agent root span. */
function rootSpanAttributes() {
  return {
    "user.id": "user-9",
    "conversation.id": "conv-123",
    "operation.id": "op-1",
    "entity.id": "assistant",
    "entity.type": "agent",
    "entity.name": "assistant",
    "agent.state": "completed",
    input: "SECRET_PROMPT",
    output: "SECRET_ANSWER",
    "agent.instructions": "SECRET_INSTRUCTIONS",
    "agent.messages": '[{"role":"user","content":"SECRET_PROMPT"}]',
    "agent.messages.ui": "[]",
    "agent.stateSnapshot": '{"instructions":"SECRET_INSTRUCTIONS"}',
    "ai.model.name": "openai/gpt-4o-mini",
    "ai.model.provider": "openai",
    "ai.model.temperature": 0.2,
    "ai.model.max_tokens": 256,
    "ai.response.finish_reason": "stop",
    "usage.prompt_tokens": 24,
    "usage.completion_tokens": 12,
    "usage.total_tokens": 36,
    "usage.cached_tokens": 3,
    "usage.reasoning_tokens": 0,
  };
}

/** Keys agent.ts (2.11.0) writes on an `llm:<operation>` span. */
function llmSpanAttributes(overrides: Record<string, unknown> = {}) {
  return {
    "user.id": "user-9",
    "conversation.id": "conv-123",
    "operation.id": "op-1",
    "entity.type": "agent",
    "span.type": "llm",
    "llm.operation": "generateText",
    "llm.model": "openai/gpt-4o-mini",
    "llm.provider": "openai",
    "llm.temperature": 0.2,
    "llm.max_output_tokens": 256,
    "llm.messages": '[{"role":"user","content":"SECRET_PROMPT"}]',
    "llm.messages.count": 2,
    "llm.usage.prompt_tokens": 13,
    "llm.usage.completion_tokens": 5,
    "llm.usage.total_tokens": 18,
    "llm.usage.cached_tokens": 2,
    "llm.usage.reasoning_tokens": 4,
    "llm.finish_reason": "stop",
    ...overrides,
  };
}

describe("resolveSpanKind (AC-03)", () => {
  it.each([
    [{ "span.type": "agent" }, FISpanKind.AGENT],
    [{ "span.type": "llm" }, FISpanKind.LLM],
    [{ "span.type": "tool" }, FISpanKind.TOOL],
    [{ "span.type": "retriever" }, FISpanKind.RETRIEVER],
    [{ "span.type": "vector" }, FISpanKind.RETRIEVER],
    [{ "span.type": "embedding" }, FISpanKind.EMBEDDING],
    [{ "span.type": "memory", "memory.operation": "read" }, FISpanKind.RETRIEVER],
    [{ "span.type": "memory", "memory.operation": "write" }, FISpanKind.CHAIN],
    [{ "span.type": "memory", "memory.operation": "write_steps" }, FISpanKind.CHAIN],
    [{ "span.type": "guardrail" }, FISpanKind.CHAIN],
    [{ "span.type": "middleware" }, FISpanKind.CHAIN],
    [{ "span.type": "summary" }, FISpanKind.CHAIN],
    [{ "span.type": "workflow-step" }, FISpanKind.CHAIN],
    [{ "span.type": "something-new" }, FISpanKind.CHAIN],
    [{ "entity.type": "agent" }, FISpanKind.AGENT],
    [{ "entity.type": "workflow" }, FISpanKind.CHAIN],
    // span.type wins over the inherited entity.type on child spans.
    [{ "entity.type": "agent", "span.type": "tool" }, FISpanKind.TOOL],
  ])("%j -> %s", (attributes, kind) => {
    expect(resolveSpanKind(attributes)).toBe(kind);
  });

  it("leaves spans without VoltAgent type keys untyped", () => {
    expect(resolveSpanKind({ "http.method": "GET" })).toBeUndefined();
    const mapped = mapVoltAgentAttributes({ "http.method": "GET" }, OFF);
    expect(mapped.attributes["fi.span.kind"]).toBeUndefined();
    expect(mapped.attributes["http.method"]).toBe("GET");
  });

  it("sets fi.span.kind, gen_ai.span.kind and openinference.span.kind together", () => {
    const { attributes } = mapVoltAgentAttributes({ "span.type": "tool", "tool.name": "x" }, OFF);
    expect(attributes["fi.span.kind"]).toBe("TOOL");
    expect(attributes["gen_ai.span.kind"]).toBe("TOOL");
    expect(attributes["openinference.span.kind"]).toBe("TOOL");
    expect(attributes["voltagent.span_type"]).toBe("tool");
  });

  it("does not override a kind that is already set", () => {
    const { attributes } = mapVoltAgentAttributes({ "span.type": "tool", "fi.span.kind": "GUARDRAIL" }, OFF);
    expect(attributes["fi.span.kind"]).toBe("GUARDRAIL");
  });
});

describe("mapVoltAgentAttributes (AC-02)", () => {
  it("maps the agent root span: session, user, model, provider; summed usage off promoted keys", () => {
    const source = rootSpanAttributes();
    const before = JSON.stringify(source);
    const mapped = mapVoltAgentAttributes(source, OFF);
    const a = mapped.attributes;

    expect(JSON.stringify(source)).toBe(before); // input not mutated
    expect(mapped.kind).toBe(FISpanKind.AGENT);
    expect(mapped.isOperationRoot).toBe(true);
    expect(mapped.isModelCall).toBe(false);
    expect(mapped.operationId).toBe("op-1");

    expect(a["fi.span.kind"]).toBe("AGENT");
    expect(a["gen_ai.operation.name"]).toBe("invoke_agent");
    expect(a["session.id"]).toBe("conv-123");
    expect(a["gen_ai.conversation.id"]).toBe("conv-123");
    expect(a["user.id"]).toBe("user-9");
    expect(a["gen_ai.request.model"]).toBe("openai/gpt-4o-mini");
    expect(a["gen_ai.response.model"]).toBe("openai/gpt-4o-mini");
    expect(a["gen_ai.provider.name"]).toBe("openai");
    expect(a["gen_ai.request.temperature"]).toBe(0.2);
    expect(a["gen_ai.request.max_tokens"]).toBe(256);
    expect(a["gen_ai.response.finish_reasons"]).toEqual(["stop"]);

    // Summed usage is namespaced, never on a promoted key.
    expect(a["voltagent.usage.input_tokens"]).toBe(24);
    expect(a["voltagent.usage.output_tokens"]).toBe(12);
    expect(a["voltagent.usage.total_tokens"]).toBe(36);
    expect(a["voltagent.usage.cache_read_tokens"]).toBe(3);
    for (const key of Object.keys(a)) expect(isPromotedUsageKey(key)).toBe(false);

    // Copy, do not delete.
    expect(a["entity.type"]).toBe("agent");
    expect(a["usage.prompt_tokens"]).toBe(24);
    expect(a["ai.model.name"]).toBe("openai/gpt-4o-mini");
    expect(a["conversation.id"]).toBe("conv-123");
  });

  it("maps llm span usage onto the promoted GenAI keys", () => {
    const mapped = mapVoltAgentAttributes(llmSpanAttributes(), OFF);
    const a = mapped.attributes;
    expect(mapped.isModelCall).toBe(true);
    expect(a["fi.span.kind"]).toBe("LLM");
    expect(a["gen_ai.operation.name"]).toBe("chat");
    expect(a["gen_ai.request.model"]).toBe("openai/gpt-4o-mini");
    expect(a["gen_ai.provider.name"]).toBe("openai");
    expect(a["gen_ai.request.max_tokens"]).toBe(256);
    expect(a["gen_ai.usage.input_tokens"]).toBe(13);
    expect(a["gen_ai.usage.output_tokens"]).toBe(5);
    expect(a["gen_ai.usage.total_tokens"]).toBe(18);
    expect(a["gen_ai.usage.cache_read.input_tokens"]).toBe(2);
    expect(a["gen_ai.usage.cache_read_tokens"]).toBe(2);
    expect(a["gen_ai.usage.reasoning.output_tokens"]).toBe(4);
    expect(a["gen_ai.usage.output_tokens.reasoning"]).toBe(4);
    expect(a["llm.usage.prompt_tokens"]).toBe(13); // original kept
    expect(a["llm.messages.count"]).toBe(2); // counts are not content
  });

  it("does not overwrite an existing session.id", () => {
    const { attributes } = mapVoltAgentAttributes({ ...rootSpanAttributes(), "session.id": "explicit" }, OFF);
    expect(attributes["session.id"]).toBe("explicit");
    expect(attributes["gen_ai.conversation.id"]).toBe("conv-123");
  });

  it("maps tool name, call id and description", () => {
    const { attributes } = mapVoltAgentAttributes(
      {
        "span.type": "tool",
        "tool.name": "get_weather",
        "tool.call.id": "call-1",
        "tool.description": "weather",
        input: '{"city":"SECRET"}',
      },
      OFF,
    );
    expect(attributes["gen_ai.tool.name"]).toBe("get_weather");
    expect(attributes["gen_ai.tool.call.id"]).toBe("call-1");
    expect(attributes["gen_ai.tool.description"]).toBe("weather");
    expect(attributes["gen_ai.operation.name"]).toBe("execute_tool");
    expect(attributes.input).toBeUndefined();
  });

  it("moves promoted token/cost keys off spans that are not model calls", () => {
    const { attributes } = mapVoltAgentAttributes(
      {
        "entity.type": "agent",
        "agent.state": "completed",
        "gen_ai.usage.input_tokens": 99,
        "llm.usage.prompt_tokens": 98,
        "llm.token_count.prompt": 97,
        "gen_ai.cost.total": 0.5,
        "llm.cost.total": 0.4,
      },
      OFF,
    );
    for (const key of Object.keys(attributes)) expect(isPromotedUsageKey(key)).toBe(false);
    expect(attributes["voltagent.gen_ai.usage.input_tokens"]).toBe(99);
    expect(attributes["voltagent.llm.usage.prompt_tokens"]).toBe(98);
    expect(attributes["voltagent.gen_ai.cost.total"]).toBe(0.5);
  });
});

describe("content (AC-07)", () => {
  const contentCarrier = {
    ...rootSpanAttributes(),
    "llm.messages": "SECRET",
    "agent.context": "SECRET",
    "middleware.input.original": "SECRET",
    "guardrail.output.after": "SECRET",
    "tool.search.query": "SECRET",
    "workspace.search.query": "SECRET",
    "vector.query": "SECRET",
    "embedding.query": "SECRET",
    "workflow.resume.data": "SECRET",
    "suspension.checkpoint": "SECRET",
    "workspace.sandbox.command": "SECRET",
  };

  it("drops prompts, messages, instructions, tool payloads and queries by default", () => {
    const { attributes } = mapVoltAgentAttributes(contentCarrier, OFF);
    expect(JSON.stringify(attributes)).not.toContain("SECRET");
    expect(attributes["input.value"]).toBeUndefined();
    expect(attributes["output.value"]).toBeUndefined();
  });

  it("copies input/output to input.value/output.value only after opt-in", () => {
    const { attributes } = mapVoltAgentAttributes(rootSpanAttributes(), ON);
    expect(attributes["input.value"]).toBe("SECRET_PROMPT");
    expect(attributes["input.mime_type"]).toBe("text/plain");
    expect(attributes["output.value"]).toBe("SECRET_ANSWER");
    expect(attributes.input).toBe("SECRET_PROMPT");
    expect(attributes["agent.instructions"]).toBe("SECRET_INSTRUCTIONS");
  });

  it("uses the vector/embedding query as retriever input only after opt-in", () => {
    const off = mapVoltAgentAttributes({ "span.type": "vector", "vector.query": "find cats" }, OFF);
    expect(off.attributes["input.value"]).toBeUndefined();
    const on = mapVoltAgentAttributes({ "span.type": "vector", "vector.query": "find cats" }, ON);
    expect(on.attributes["input.value"]).toBe("find cats");
    expect(on.attributes["fi.span.kind"]).toBe("RETRIEVER");
  });

  it("classifies keys", () => {
    expect(isContentKey("input", "x")).toBe(true);
    expect(isContentKey("llm.messages", "x")).toBe(true);
    expect(isContentKey("llm.messages.count", 2)).toBe(false);
    expect(isContentKey("memory.context_limit", 10)).toBe(false);
    expect(isContentKey("gen_ai.usage.input_tokens", 3)).toBe(false);
    expect(isContentKey("tool.name", "x")).toBe(false);
  });
});

describe("credentials", () => {
  it("never exports API keys or headers, even with captureContent", () => {
    const { attributes } = mapVoltAgentAttributes(
      {
        "span.type": "llm",
        "llm.api_key": "sk-PLACEHOLDER",
        "http.request.headers": "x-api-key: PLACEHOLDER",
        "provider.authorization": "Bearer PLACEHOLDER",
        "voltops.secret_key": "PLACEHOLDER",
      },
      ON,
    );
    expect(JSON.stringify(attributes)).not.toContain("PLACEHOLDER");
    expect(isSecretKey("usage.total_tokens")).toBe(false);
  });
});

describe("reconcileOperationUsage", () => {
  const held = (overrides: Record<string, unknown> = {}, isError = false) => {
    const source = llmSpanAttributes(overrides);
    return { source, attributes: mapVoltAgentAttributes(source, OFF).attributes, isError };
  };

  it("puts the operation total on the single main llm span and keeps the last step", () => {
    const span = held();
    const result = reconcileOperationUsage(rootSpanAttributes(), [span]);
    expect(result).toBe(span.attributes);
    const a = span.attributes;
    expect(a["gen_ai.usage.input_tokens"]).toBe(24);
    expect(a["gen_ai.usage.output_tokens"]).toBe(12);
    expect(a["gen_ai.usage.total_tokens"]).toBe(36);
    expect(a["llm.usage.prompt_tokens"]).toBe(24);
    expect(a["llm.usage.completion_tokens"]).toBe(12);
    expect(a["gen_ai.usage.cache_read.input_tokens"]).toBe(3);
    expect(a["gen_ai.usage.reasoning.output_tokens"]).toBeUndefined(); // root reported 0
    expect(a["llm.usage.reasoning_tokens"]).toBeUndefined();
    expect(a["voltagent.llm.last_step_usage.prompt_tokens"]).toBe(13);
    expect(a["voltagent.llm.last_step_usage.completion_tokens"]).toBe(5);
    expect(a["voltagent.usage.reconciled"]).toBe(true);
  });

  it("leaves single-step calls alone", () => {
    const span = held({ "llm.usage.prompt_tokens": 24, "llm.usage.completion_tokens": 12 });
    expect(reconcileOperationUsage(rootSpanAttributes(), [span])).toBeUndefined();
    expect(span.attributes["gen_ai.usage.input_tokens"]).toBe(24);
  });

  it("does nothing when the main call is ambiguous or failed", () => {
    expect(reconcileOperationUsage(rootSpanAttributes(), [held(), held()])).toBeUndefined();
    expect(reconcileOperationUsage(rootSpanAttributes(), [held({}, true)])).toBeUndefined();
    expect(
      reconcileOperationUsage(rootSpanAttributes(), [held({ "llm.operation": "generateTitle" })]),
    ).toBeUndefined();
    const { "usage.prompt_tokens": _p, "usage.completion_tokens": _c, ...noUsage } = rootSpanAttributes();
    expect(reconcileOperationUsage(noUsage, [held()])).toBeUndefined();
  });

  it("reconciles the one main call even when a title-generation call is present", () => {
    const main = held();
    const title = held({ "llm.operation": "generateTitle", "llm.usage.prompt_tokens": 4 });
    expect(reconcileOperationUsage(rootSpanAttributes(), [main, title])).toBe(main.attributes);
    expect(title.attributes["gen_ai.usage.input_tokens"]).toBe(4);
  });
});
