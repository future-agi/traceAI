import { FISpanKind } from "@traceai/fi-semantic-conventions";
import {
  GENKIT_ACTION_SUBTYPES,
  GENKIT_CONTENT_KEYS,
  GENKIT_USAGE_NOT_MAPPED,
  SUBTYPE_TO_KIND,
  USAGE_TO_ATTRIBUTE,
  genkitSpanKind,
  isPromotedUsageKey,
  mapGenkitAttributes,
} from "../index";
import {
  OUTPUT_MARKER,
  PROMPT_MARKER,
  TOOL_OUTPUT_MARKER,
  flowAttributes,
  flowStepAttributes,
  generateAttributes,
  modelAttributes,
  toolAttributes,
} from "./fixtures";

const kindOf = (attributes: Record<string, unknown>) => [attributes["fi.span.kind"], attributes["gen_ai.span.kind"]];

describe("genkitSpanKind: kind map from the 1.42.0 type-string inventory", () => {
  it.each([
    ["flow", FISpanKind.CHAIN],
    ["model", FISpanKind.LLM],
    ["background-model", FISpanKind.LLM],
    ["tool", FISpanKind.TOOL],
    ["tool.v2", FISpanKind.TOOL],
    ["retriever", FISpanKind.RETRIEVER],
    ["embedder", FISpanKind.EMBEDDING],
    ["reranker", FISpanKind.RERANKER],
    ["evaluator", FISpanKind.EVALUATOR],
    ["agent", FISpanKind.AGENT],
  ])("genkit:type=action, subtype=%s -> %s", (subtype, kind) => {
    expect(genkitSpanKind({ "genkit:type": "action", "genkit:metadata:subtype": subtype })).toBe(kind);
  });

  it("maps every other 1.42.0 action subtype to CHAIN", () => {
    const others = GENKIT_ACTION_SUBTYPES.filter((s) => SUBTYPE_TO_KIND[s] === undefined);
    expect(others.sort()).toEqual(
      [
        "agent-abort",
        "agent-snapshot",
        "cancel-operation",
        "check-operation",
        "custom",
        "dynamic-action-provider",
        "executable-prompt",
        "indexer",
        "prompt",
        "resource",
        "util",
      ].sort(),
    );
    for (const subtype of others) {
      expect(genkitSpanKind({ "genkit:type": "action", "genkit:metadata:subtype": subtype })).toBe(FISpanKind.CHAIN);
    }
  });

  it.each(["flowStep", "util", "promptTemplate", "dotprompt"])("genkit:type=%s -> CHAIN", (type) => {
    expect(genkitSpanKind({ "genkit:type": type })).toBe(FISpanKind.CHAIN);
  });

  it("leaves spans without a known genkit:type unmapped", () => {
    expect(genkitSpanKind({})).toBeUndefined();
    expect(genkitSpanKind({ "genkit:type": "somethingNew" })).toBeUndefined();
    expect(genkitSpanKind({ "http.method": "GET" })).toBeUndefined();
    const out = mapGenkitAttributes({ "genkit:type": "somethingNew", "genkit:name": "x" });
    expect(out["fi.span.kind"]).toBeUndefined();
    expect(out["gen_ai.span.kind"]).toBeUndefined();
    expect(out["genkit:name"]).toBe("x");
  });

  it("writes both fi.span.kind and gen_ai.span.kind", () => {
    expect(kindOf(mapGenkitAttributes(flowAttributes()))).toEqual(["CHAIN", "CHAIN"]);
    expect(kindOf(mapGenkitAttributes(modelAttributes()))).toEqual(["LLM", "LLM"]);
    expect(kindOf(mapGenkitAttributes(toolAttributes()))).toEqual(["TOOL", "TOOL"]);
    expect(kindOf(mapGenkitAttributes(generateAttributes()))).toEqual(["CHAIN", "CHAIN"]);
    expect(kindOf(mapGenkitAttributes(flowStepAttributes()))).toEqual(["CHAIN", "CHAIN"]);
  });

  it("does not overwrite a kind already on the span", () => {
    const out = mapGenkitAttributes({ ...toolAttributes(), "fi.span.kind": "AGENT" });
    expect(out["fi.span.kind"]).toBe("AGENT");
    expect(out["gen_ai.span.kind"]).toBe("TOOL");
  });
});

describe("model call spans", () => {
  it("copies model name, usage and finish reason from the model action span", () => {
    const out = mapGenkitAttributes(modelAttributes());
    expect(out["gen_ai.request.model"]).toBe("fixture/mock");
    expect(out["gen_ai.usage.input_tokens"]).toBe(11);
    expect(out["gen_ai.usage.output_tokens"]).toBe(3);
    expect(out["gen_ai.usage.total_tokens"]).toBe(14);
    expect(out["gen_ai.response.finish_reasons"]).toEqual(["stop"]);
  });

  it("copies only the usage fields the inventory observed (input/output/total tokens)", () => {
    expect(USAGE_TO_ATTRIBUTE).toEqual({
      inputTokens: "gen_ai.usage.input_tokens",
      outputTokens: "gen_ai.usage.output_tokens",
      totalTokens: "gen_ai.usage.total_tokens",
    });
  });

  it("leaves GenerationUsage fields the inventory did not observe unavailable (not copied)", () => {
    // thoughtsTokens, cachedContentTokens and the character/media counters are in
    // GenerationUsageSchema (ai/src/model-types.ts:260-275) but no span the inventory
    // dumped carried them, so they are not mapped.
    const out = mapGenkitAttributes(
      modelAttributes({
        inputTokens: 5,
        outputTokens: 6,
        totalTokens: 11,
        thoughtsTokens: 2,
        cachedContentTokens: 4,
        inputCharacters: 99,
        outputImages: 1,
      }),
    );
    expect(Object.keys(out).filter(isPromotedUsageKey).sort()).toEqual(
      ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens"].sort(),
    );
    expect(out["gen_ai.usage.output_tokens.reasoning"]).toBeUndefined();
    expect(out["gen_ai.usage.cache_read_tokens"]).toBeUndefined();
    expect(Object.keys(out).some((k) => /character|image/i.test(k))).toBe(false);
    expect(GENKIT_USAGE_NOT_MAPPED).toEqual(
      expect.arrayContaining(["thoughtsTokens", "cachedContentTokens", "inputCharacters", "outputImages", "custom"]),
    );
  });

  it("records usage as unavailable (absent) when the model reported none", () => {
    const out = mapGenkitAttributes(modelAttributes(null));
    for (const key of Object.keys(out)) {
      expect(isPromotedUsageKey(key)).toBe(false);
    }
    expect(out["gen_ai.request.model"]).toBe("fixture/mock");
  });

  it("does not throw on a truncated or non-JSON genkit:output", () => {
    const attributes = { ...modelAttributes(), "genkit:output": '{"message":{"role":"model","content":[{"te' };
    const out = mapGenkitAttributes(attributes);
    expect(out["gen_ai.usage.input_tokens"]).toBeUndefined();
    expect(out["fi.span.kind"]).toBe("LLM");
  });

  it("puts promoted token keys only on model spans, never on flow / generate / tool spans", () => {
    for (const attributes of [flowAttributes(), generateAttributes(), toolAttributes(), flowStepAttributes()]) {
      const out = mapGenkitAttributes(attributes, { captureContent: true });
      expect(Object.keys(out).filter(isPromotedUsageKey)).toEqual([]);
    }
  });

  it("moves aggregate usage found on a non-model span to genkit.usage.*", () => {
    const out = mapGenkitAttributes({
      ...flowAttributes(),
      "gen_ai.usage.input_tokens": 32,
      "llm.token_count.prompt": 32,
      "llm.usage.total_tokens": 40,
      "gen_ai.cost.total": 0.5,
      "llm.cost.total": 0.5,
    });
    expect(Object.keys(out).filter(isPromotedUsageKey)).toEqual([]);
    expect(out["genkit.usage.gen_ai.usage.input_tokens"]).toBe(32);
    expect(out["genkit.usage.llm.token_count.prompt"]).toBe(32);
    expect(out["genkit.usage.llm.usage.total_tokens"]).toBe(40);
    expect(out["genkit.usage.gen_ai.cost.total"]).toBe(0.5);
    expect(out["genkit.usage.llm.cost.total"]).toBe(0.5);
  });

  it("matches the collector's promoted-key families", () => {
    for (const key of [
      "gen_ai.usage.input_tokens",
      "gen_ai.usage.output_tokens",
      "gen_ai.usage.total_tokens",
      "llm.token_count.prompt",
      "llm.usage.prompt_tokens",
      "gen_ai.cost.total",
      "llm.cost.total",
    ]) {
      expect(isPromotedUsageKey(key)).toBe(true);
    }
    for (const key of ["genkit.usage.gen_ai.usage.input_tokens", "genkit:output", "gen_ai.request.model"]) {
      expect(isPromotedUsageKey(key)).toBe(false);
    }
  });
});

describe("tool spans", () => {
  it("copies the tool action name to tool.name and gen_ai.tool.name", () => {
    const out = mapGenkitAttributes(toolAttributes());
    expect(out["tool.name"]).toBe("lookup");
    expect(out["gen_ai.tool.name"]).toBe("lookup");
  });
});

describe("content", () => {
  const all = () => [flowAttributes(), flowStepAttributes(), generateAttributes(), modelAttributes(), toolAttributes()];

  it("drops genkit:input/output/init/interrupt/resumed by default", () => {
    for (const attributes of all()) {
      const out = mapGenkitAttributes({
        ...attributes,
        "genkit:init": "{}",
        "genkit:metadata:interrupt": "{}",
        "genkit:metadata:resumed": "{}",
      });
      for (const key of GENKIT_CONTENT_KEYS) expect(out[key]).toBeUndefined();
      expect(out["input.value"]).toBeUndefined();
      expect(out["output.value"]).toBeUndefined();
      const blob = JSON.stringify(out);
      for (const marker of [PROMPT_MARKER, OUTPUT_MARKER, TOOL_OUTPUT_MARKER]) expect(blob).not.toContain(marker);
    }
  });

  it("never exports genkit:metadata:context, even with captureContent", () => {
    for (const captureContent of [false, true]) {
      const out = mapGenkitAttributes(flowAttributes(), { captureContent });
      expect(out["genkit:metadata:context"]).toBeUndefined();
      expect(JSON.stringify(out)).not.toContain("SECRET_HEADER_MARKER");
    }
  });

  it("exports input and output after opt-in", () => {
    const out = mapGenkitAttributes(toolAttributes(), { captureContent: true });
    expect(out["input.value"]).toBe(JSON.stringify({ id: 7 }));
    expect(out["output.value"]).toContain(TOOL_OUTPUT_MARKER);
    expect(out["input.mime_type"]).toBe("application/json");
    expect(out["output.mime_type"]).toBe("application/json");
    expect(out["genkit:output"]).toContain(TOOL_OUTPUT_MARKER);
  });

  it("keeps usage on model spans with content off (usage is read before the output is dropped)", () => {
    const out = mapGenkitAttributes(modelAttributes());
    expect(out["genkit:output"]).toBeUndefined();
    expect(out["gen_ai.usage.total_tokens"]).toBe(14);
  });

  it("never modifies the source attributes", () => {
    const source = modelAttributes();
    const before = JSON.stringify(source);
    mapGenkitAttributes(source);
    mapGenkitAttributes(source, { captureContent: true });
    expect(JSON.stringify(source)).toBe(before);
  });
});

describe("session and context attributes", () => {
  it("maps the beta agent session id to session.id", () => {
    const out = mapGenkitAttributes({
      "genkit:type": "action",
      "genkit:metadata:subtype": "agent",
      "genkit:name": "supportAgent",
      "genkit:metadata:agent:sessionId": "sess-agent-1",
    });
    expect(out["session.id"]).toBe("sess-agent-1");
    expect(out["genkit:metadata:agent:sessionId"]).toBe("sess-agent-1");
    expect(out["fi.span.kind"]).toBe("AGENT");
  });

  it("applies fi-core context attributes without overriding span keys", () => {
    const out = mapGenkitAttributes(
      { ...flowAttributes(), "user.id": "from-span" },
      { contextAttributes: { "session.id": "sess-ctx", "user.id": "from-context", "tag.tags": ["a"] } },
    );
    expect(out["session.id"]).toBe("sess-ctx");
    expect(out["user.id"]).toBe("from-span");
    expect(out["tag.tags"]).toEqual(["a"]);
  });

  it("does not let context attributes put promoted usage on a non-model span", () => {
    const out = mapGenkitAttributes(flowAttributes(), { contextAttributes: { "gen_ai.usage.input_tokens": 3 } });
    expect(out["gen_ai.usage.input_tokens"]).toBeUndefined();
    expect(out["genkit.usage.gen_ai.usage.input_tokens"]).toBe(3);
  });

  it("sets no session.id when Genkit has none", () => {
    expect(mapGenkitAttributes(flowAttributes())["session.id"]).toBeUndefined();
  });
});
