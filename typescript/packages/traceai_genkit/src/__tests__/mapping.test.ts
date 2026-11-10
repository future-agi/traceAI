import { FISpanKind } from "@traceai/fi-semantic-conventions";
import {
  GENKIT_ACTION_SUBTYPES,
  GENKIT_CONTENT_KEYS,
  GENKIT_INPUT_KEYS,
  GENKIT_OUTPUT_KEYS,
  HIDE_ALL,
  SHOW_ALL,
  type ContentPolicy,
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
    expect(out["gen_ai.request.model"]).toBe("mock");
    expect(out["gen_ai.usage.input_tokens"]).toBe(11);
    expect(out["gen_ai.usage.output_tokens"]).toBe(3);
    expect(out["gen_ai.usage.total_tokens"]).toBe(14);
    expect(out["gen_ai.response.finish_reasons"]).toEqual(["stop"]);
  });

  it("copies input/output/total tokens, plus the reasoning and cached-prompt counts real Gemini reports", () => {
    expect(USAGE_TO_ATTRIBUTE).toEqual({
      inputTokens: "gen_ai.usage.input_tokens",
      outputTokens: "gen_ai.usage.output_tokens",
      totalTokens: "gen_ai.usage.total_tokens",
      thoughtsTokens: "gen_ai.usage.output_tokens.reasoning",
      cachedContentTokens: "gen_ai.usage.cache_read_tokens",
    });
  });

  it("maps thoughtsTokens and cachedContentTokens (set by @genkit-ai/google-genai) on model spans", () => {
    // @genkit-ai/google-genai 1.42.0 lib/googleai/gemini.js:642-646 copies
    // usageMetadata.thoughtsTokenCount / cachedContentTokenCount. A real
    // gemini-3.8-flash call reported inputTokens 66, outputTokens 15,
    // totalTokens 133: the 52 thinking tokens are only in thoughtsTokens.
    const out = mapGenkitAttributes(
      modelAttributes({ inputTokens: 66, outputTokens: 15, totalTokens: 133, thoughtsTokens: 52, cachedContentTokens: 8 }),
    );
    expect(out["gen_ai.usage.output_tokens.reasoning"]).toBe(52);
    expect(out["gen_ai.usage.cache_read_tokens"]).toBe(8);
    expect(out["gen_ai.usage.output_tokens"]).toBe(15);
    expect(out["gen_ai.usage.total_tokens"]).toBe(133);
  });

  it("leaves the other GenerationUsage fields unavailable (not copied)", () => {
    // The character/media counters and `custom` are in GenerationUsageSchema
    // (ai/src/model-types.ts:260-275) but neither the inventory nor the
    // google-genai plugin sets them, so they are not mapped.
    const out = mapGenkitAttributes(
      modelAttributes({
        inputTokens: 5,
        outputTokens: 6,
        totalTokens: 11,
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
    expect(GENKIT_USAGE_NOT_MAPPED).toEqual(expect.arrayContaining(["inputCharacters", "outputImages", "custom"]));
    expect(GENKIT_USAGE_NOT_MAPPED).not.toContain("thoughtsTokens");
    expect(GENKIT_USAGE_NOT_MAPPED).not.toContain("cachedContentTokens");
  });

  it.each([
    ["googleai/gemini-3.5-flash", "gemini-3.5-flash", "google"],
    ["vertexai/gemini-3.5-flash", "gemini-3.5-flash", "vertexai"],
    ["vertexai/claude-sonnet-5", "claude-sonnet-5", "vertexai"],
    ["openai/gpt-4o-mini", "gpt-4o-mini", "openai"],
    ["anthropic/claude-haiku-4-5", "claude-haiku-4-5", "anthropic"],
    ["deepseek/deepseek-chat", "deepseek-chat", "deepseek"],
    ["xai/grok-4.5", "grok-4.5", "xai"],
    ["ollama/llama3.2", "llama3.2", "ollama"],
    ["mistral/mistral-large-latest", "mistral-large-latest", "mistralai"],
    ["groq/llama-3.3-70b-versatile", "llama-3.3-70b-versatile", "groq"],
    ["aws-bedrock/amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0", "aws"],
    ["azure-openai/gpt-4o", "gpt-4o", "azure"],
  ])("%s: model %s, provider %s (the collector prices by the bare model name)", (genkitName, model, provider) => {
    const out = mapGenkitAttributes({ ...modelAttributes(), "genkit:name": genkitName });
    expect(out["gen_ai.request.model"]).toBe(model);
    expect(out["gen_ai.response.model"]).toBe(model);
    expect(out["gen_ai.provider.name"]).toBe(provider);
    // The full registered name stays on the passed-through Genkit key.
    expect(out["genkit:name"]).toBe(genkitName);
  });

  it("strips an unknown plugin namespace but leaves the provider unset", () => {
    const out = mapGenkitAttributes({ ...modelAttributes(), "genkit:name": "together/meta-llama/Llama-3-70b" });
    expect(out["gen_ai.request.model"]).toBe("meta-llama/Llama-3-70b");
    expect(out["gen_ai.provider.name"]).toBeUndefined();
  });

  it("keeps a model name with no namespace as is, with no provider", () => {
    const out = mapGenkitAttributes({ ...modelAttributes(), "genkit:name": "my-model" });
    expect(out["gen_ai.request.model"]).toBe("my-model");
    expect(out["gen_ai.provider.name"]).toBeUndefined();
  });

  it("sets no model or provider on non-model spans", () => {
    const out = mapGenkitAttributes({ ...toolAttributes(), "genkit:name": "googleai/gemini-3.5-flash" });
    expect(out["gen_ai.request.model"]).toBeUndefined();
    expect(out["gen_ai.provider.name"]).toBeUndefined();
  });

  it("records usage as unavailable (absent) when the model reported none", () => {
    const out = mapGenkitAttributes(modelAttributes(null));
    for (const key of Object.keys(out)) {
      expect(isPromotedUsageKey(key)).toBe(false);
    }
    expect(out["gen_ai.request.model"]).toBe("mock");
  });

  it("does not throw on a truncated or non-JSON genkit:output", () => {
    const attributes = { ...modelAttributes(), "genkit:output": '{"message":{"role":"model","content":[{"te' };
    const out = mapGenkitAttributes(attributes);
    expect(out["gen_ai.usage.input_tokens"]).toBeUndefined();
    expect(out["fi.span.kind"]).toBe("LLM");
  });

  it("puts promoted token keys only on model spans, never on flow / generate / tool spans", () => {
    for (const attributes of [flowAttributes(), generateAttributes(), toolAttributes(), flowStepAttributes()]) {
      const out = mapGenkitAttributes(attributes);
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

  it("treats every gen_ai.cost.* and llm.cost.* key as promoted, cost parts included", () => {
    // fi-collector promotes the cost parts too when no total is present (DeriveHotKeys).
    for (const key of [
      "gen_ai.cost.input",
      "gen_ai.cost.output",
      "gen_ai.cost.total",
      "llm.cost.prompt",
      "llm.cost.completion",
      "llm.cost.total",
      "gen_ai.cost.some_future_part",
    ]) {
      expect(isPromotedUsageKey(key)).toBe(true);
    }
    for (const key of ["genkit.usage.gen_ai.cost.input", "gen_ai.costs", "llm.costing", "cost.total"]) {
      expect(isPromotedUsageKey(key)).toBe(false);
    }
  });

  it("moves cost parts on a non-model span to genkit.usage.*, from span and context attributes", () => {
    const out = mapGenkitAttributes(
      { ...flowAttributes(), "gen_ai.cost.input": 0.01, "llm.cost.completion": 0.02 },
      { contextAttributes: { "gen_ai.cost.output": 0.03, "llm.cost.prompt": 0.04 } },
    );
    expect(Object.keys(out).filter((k) => k.startsWith("gen_ai.cost.") || k.startsWith("llm.cost."))).toEqual([]);
    expect(out["genkit.usage.gen_ai.cost.input"]).toBe(0.01);
    expect(out["genkit.usage.llm.cost.completion"]).toBe(0.02);
    expect(out["genkit.usage.gen_ai.cost.output"]).toBe(0.03);
    expect(out["genkit.usage.llm.cost.prompt"]).toBe(0.04);
  });

  it("keeps cost parts unprefixed on a model span", () => {
    const out = mapGenkitAttributes(modelAttributes(), { contextAttributes: { "gen_ai.cost.input": 0.05 } });
    expect(out["gen_ai.cost.input"]).toBe(0.05);
    expect(out["genkit.usage.gen_ai.cost.input"]).toBeUndefined();
  });
});

describe("embedder spans", () => {
  // Shape of a real @genkit-ai/google-genai 1.42.0 embedder span (gemini-embedding-001), vector shortened.
  const embedderAttributes = () => ({
    "genkit:type": "action",
    "genkit:metadata:subtype": "embedder",
    "genkit:name": "googleai/gemini-embedding-001",
    "genkit:input": JSON.stringify({ input: [{ content: [{ text: PROMPT_MARKER }] }], options: {} }),
    "genkit:output": JSON.stringify({ embeddings: [{ embedding: [0.0145, -0.0166, 0.0121] }] }),
    "genkit:state": "success",
  });

  it("sets EMBEDDING kind, the bare embedding.model_name and the provider", () => {
    const out = mapGenkitAttributes(embedderAttributes());
    expect(out["fi.span.kind"]).toBe("EMBEDDING");
    expect(out["embedding.model_name"]).toBe("gemini-embedding-001");
    expect(out["gen_ai.provider.name"]).toBe("google");
    expect(out["gen_ai.request.model"]).toBeUndefined();
    expect(out["output.value"]).toContain("0.0145");
  });

  it("FI_HIDE_EMBEDDING_VECTORS replaces the vectors and keeps the rest", () => {
    const out = mapGenkitAttributes(embedderAttributes(), { policy: { ...SHOW_ALL, hideEmbeddingVectors: true } });
    expect(JSON.parse(String(out["output.value"]))).toEqual({ embeddings: [{ embedding: "__REDACTED__" }] });
    expect(out["genkit:output"]).toBe(out["output.value"]);
    expect(out["input.value"]).toContain(PROMPT_MARKER);
    // Only embedder outputs are touched.
    const tool = mapGenkitAttributes(toolAttributes(), { policy: { ...SHOW_ALL, hideEmbeddingVectors: true } });
    expect(tool["output.value"]).toContain(TOOL_OUTPUT_MARKER);
  });
});

describe("tool spans", () => {
  it("copies the tool action name to tool.name and gen_ai.tool.name", () => {
    const out = mapGenkitAttributes(toolAttributes());
    expect(out["tool.name"]).toBe("lookup");
    expect(out["gen_ai.tool.name"]).toBe("lookup");
  });
});

describe("content (traceAI TraceConfig)", () => {
  const all = () => [flowAttributes(), flowStepAttributes(), generateAttributes(), modelAttributes(), toolAttributes()];
  const withExtras = (attributes: Record<string, unknown>) => ({
    ...attributes,
    "genkit:init": `{"state":"${PROMPT_MARKER}"}`,
    "genkit:metadata:interrupt": `{"ask":"${OUTPUT_MARKER}"}`,
    "genkit:metadata:resumed": `{"answer":"${PROMPT_MARKER}"}`,
  });
  const policy = (p: Partial<ContentPolicy>): ContentPolicy => ({ ...SHOW_ALL, ...p });

  it("exports input and output by default (fi-core DEFAULT_HIDE_* = false)", () => {
    const out = mapGenkitAttributes(toolAttributes());
    expect(out["input.value"]).toBe(JSON.stringify({ id: 7 }));
    expect(out["output.value"]).toContain(TOOL_OUTPUT_MARKER);
    expect(out["input.mime_type"]).toBe("application/json");
    expect(out["output.mime_type"]).toBe("application/json");
    expect(out["genkit:input"]).toBe(JSON.stringify({ id: 7 }));
    expect(out["genkit:output"]).toContain(TOOL_OUTPUT_MARKER);
  });

  it("exports the model request and response by default", () => {
    const out = mapGenkitAttributes(modelAttributes());
    expect(out["input.value"]).toContain(PROMPT_MARKER);
    expect(out["output.value"]).toContain('"toolRequest"');
  });

  it("hideInputs + hideOutputs drop every content key and set input/output.value to __REDACTED__", () => {
    for (const attributes of all()) {
      const out = mapGenkitAttributes(withExtras(attributes), { policy: policy({ hideInputs: true, hideOutputs: true }) });
      for (const key of GENKIT_CONTENT_KEYS) expect(out[key]).toBeUndefined();
      if (attributes["genkit:input"] !== undefined) expect(out["input.value"]).toBe("__REDACTED__");
      expect(out["input.mime_type"]).toBeUndefined();
      if (attributes["genkit:output"] !== undefined) expect(out["output.value"]).toBe("__REDACTED__");
      expect(out["output.mime_type"]).toBeUndefined();
      const blob = JSON.stringify(out);
      for (const marker of [PROMPT_MARKER, OUTPUT_MARKER, TOOL_OUTPUT_MARKER]) expect(blob).not.toContain(marker);
    }
  });

  it("hideInputs hides only the input side", () => {
    const out = mapGenkitAttributes(withExtras(toolAttributes()), { policy: policy({ hideInputs: true }) });
    for (const key of GENKIT_INPUT_KEYS) expect(out[key]).toBeUndefined();
    expect(out["input.value"]).toBe("__REDACTED__");
    expect(out["output.value"]).toContain(TOOL_OUTPUT_MARKER);
    expect(out["genkit:metadata:interrupt"]).toContain(OUTPUT_MARKER);
  });

  it("hideOutputs hides only the output side", () => {
    const out = mapGenkitAttributes(withExtras(toolAttributes()), { policy: policy({ hideOutputs: true }) });
    for (const key of GENKIT_OUTPUT_KEYS) expect(out[key]).toBeUndefined();
    expect(out["output.value"]).toBe("__REDACTED__");
    expect(out["input.value"]).toBe(JSON.stringify({ id: 7 }));
    expect(out["genkit:init"]).toContain(PROMPT_MARKER);
  });

  it("hideInputMessages / hideOutputMessages hide model request / response, not tool or flow content", () => {
    const p = policy({ hideInputMessages: true, hideOutputMessages: true });
    const model = mapGenkitAttributes(modelAttributes(), { policy: p });
    expect(model["genkit:input"]).toBeUndefined();
    expect(model["genkit:output"]).toBeUndefined();
    expect(model["input.value"]).toBe("__REDACTED__");
    expect(model["output.value"]).toBe("__REDACTED__");
    expect(model["gen_ai.usage.total_tokens"]).toBe(14);
    const tool = mapGenkitAttributes(toolAttributes(), { policy: p });
    expect(tool["output.value"]).toContain(TOOL_OUTPUT_MARKER);
  });

  it("hideInputText / hideOutputText redact message text on model spans and keep roles and usage", () => {
    const textModel = {
      ...modelAttributes(),
      "genkit:output": JSON.stringify({
        message: { role: "model", content: [{ text: OUTPUT_MARKER }, { reasoning: OUTPUT_MARKER }] },
        finishReason: "stop",
        usage: { inputTokens: 11, outputTokens: 3, totalTokens: 14 },
      }),
    };
    const out = mapGenkitAttributes(textModel, { policy: policy({ hideInputText: true, hideOutputText: true }) });
    const input = JSON.parse(String(out["input.value"]));
    const output = JSON.parse(String(out["output.value"]));
    expect(JSON.stringify(input)).not.toContain(PROMPT_MARKER);
    expect(JSON.stringify(output)).not.toContain(OUTPUT_MARKER);
    expect(input.messages[0].role).toBe("user");
    expect(input.messages[0].content[0].text).toBe("__REDACTED__");
    expect(output.message.role).toBe("model");
    expect(output.usage.totalTokens).toBe(14);
    expect(out["gen_ai.usage.total_tokens"]).toBe(14);
    // Not a model span: the text flags do not apply (fi-core applies them to LLM messages only).
    const tool = mapGenkitAttributes(toolAttributes(), { policy: policy({ hideInputText: true, hideOutputText: true }) });
    expect(tool["output.value"]).toContain(TOOL_OUTPUT_MARKER);
  });

  it("never exports genkit:metadata:context, in either mode", () => {
    for (const p of [SHOW_ALL, HIDE_ALL]) {
      const out = mapGenkitAttributes(flowAttributes(), { policy: p });
      expect(out["genkit:metadata:context"]).toBeUndefined();
      expect(JSON.stringify(out)).not.toContain("SECRET_HEADER_MARKER");
    }
  });

  it("keeps usage on model spans with outputs hidden (usage is read before the output is dropped)", () => {
    const out = mapGenkitAttributes(modelAttributes(), { policy: HIDE_ALL });
    expect(out["genkit:output"]).toBeUndefined();
    expect(out["gen_ai.usage.total_tokens"]).toBe(14);
    expect(out["gen_ai.response.finish_reasons"]).toEqual(["stop"]);
  });

  it("rewrites local paths and removes stack traces inside exported content, in either mode", () => {
    const scrub = { cwd: "/srv/app", home: "/home/dev" };
    const out = mapGenkitAttributes(
      {
        ...toolAttributes(),
        "genkit:output": JSON.stringify({
          file: "/srv/app/data/orders.json",
          cfg: "/home/dev/.config/x",
          other: "/Users/someone/notes.txt",
          err: "boom\n    at run (/srv/app/tool.js:1:2)",
          url: "https://example.com/v1/models",
        }),
      },
      { scrub },
    );
    const output = String(out["output.value"]);
    expect(output).not.toContain("/srv/app");
    expect(output).not.toContain("/home/dev");
    expect(output).not.toContain("/Users/someone");
    expect(output).not.toContain("tool.js:1:2");
    expect(output).toContain("[cwd]/data/orders.json");
    expect(output).toContain("~/.config/x");
    expect(output).toContain("https://example.com/v1/models");
    expect(out["genkit:output"]).toBe(output);
  });

  it("leaves Genkit action paths alone", () => {
    const out = mapGenkitAttributes(flowAttributes(), { scrub: { cwd: "/srv/app" } });
    expect(out["genkit:path"]).toBe(flowAttributes()["genkit:path"]);
  });

  it("never modifies the source attributes", () => {
    const source = modelAttributes();
    const before = JSON.stringify(source);
    mapGenkitAttributes(source);
    mapGenkitAttributes(source, { policy: HIDE_ALL });
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
