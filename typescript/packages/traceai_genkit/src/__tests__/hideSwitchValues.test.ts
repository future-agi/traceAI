import { diag } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { FIGenkitSpanProcessor, resolveContentPolicy } from "../index";
import { PROMPT_MARKER, modelAttributes, v1Span } from "./fixtures";

// A privacy switch someone set must never silently do nothing. true, 1, yes and on turn it
// on, false, 0, no and off turn it off (any case, surrounding spaces ignored), and an empty
// value counts as unset. Any other value hides, with one warning that names the variable
// and the accepted values and never repeats the value.

const saved = { ...process.env };
let warn: jest.SpyInstance;
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (key.startsWith("FI_HIDE_")) delete process.env[key];
  warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
});
afterEach(() => {
  warn.mockRestore();
  process.env = { ...saved };
});

describe("FI_HIDE_* values in the content policy", () => {
  it.each(["true", "1", "yes", "on", " TRUE ", "On", "YES"])("%j turns a hide switch on", (value) => {
    process.env.FI_HIDE_INPUTS = value;
    expect(resolveContentPolicy().hideInputs).toBe(true);
  });

  it.each(["false", "0", "no", "off", " OFF ", "No"])("%j turns a hide switch off", (value) => {
    process.env.FI_HIDE_OUTPUTS = value;
    expect(resolveContentPolicy().hideOutputs).toBe(false);
    // Off is a value someone set: it also wins over a hiding fallback.
    expect(resolveContentPolicy(undefined, { hideOutputs: true }).hideOutputs).toBe(false);
  });

  it.each(["", "   "])("%j counts as unset", (value) => {
    process.env.FI_HIDE_INPUT_MESSAGES = value;
    expect(resolveContentPolicy().hideInputMessages).toBe(false);
    expect(resolveContentPolicy(undefined, { hideInputMessages: true }).hideInputMessages).toBe(true);
  });

  it("an unrecognised value hides, with one warning that names the variable and not the value", () => {
    process.env.FI_HIDE_OUTPUT_TEXT = "maybe-later";
    expect(resolveContentPolicy().hideOutputText).toBe(true);
    expect(resolveContentPolicy().hideOutputText).toBe(true);
    const messages = warn.mock.calls.map((call) => String(call[0]));
    const named = messages.filter((message) => message.includes("FI_HIDE_OUTPUT_TEXT"));
    expect(named).toHaveLength(1);
    expect(named[0]).toContain("true, 1, yes, on");
    for (const message of messages) expect(message).not.toContain("maybe-later");
  });

  it("FI_HIDE_LLM_INVOCATION_PARAMETERS and FI_HIDE_EMBEDDING_VECTORS follow the same rule", () => {
    process.env.FI_HIDE_LLM_INVOCATION_PARAMETERS = "on";
    process.env.FI_HIDE_EMBEDDING_VECTORS = "1";
    const policy = resolveContentPolicy();
    expect(policy.hideLLMInvocationParameters).toBe(true);
    expect(policy.hideEmbeddingVectors).toBe(true);
  });

  it("an explicit option still wins over any environment value", () => {
    process.env.FI_HIDE_INPUTS = "enabled-please";
    expect(resolveContentPolicy({ hideInputs: false }).hideInputs).toBe(false);
    for (const call of warn.mock.calls) expect(String(call[0])).not.toContain("enabled-please");
  });
});

describe("FI_HIDE_* values in the span processor", () => {
  function exportedAttributes() {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({
      resource: resourceFromAttributes({ project_name: "unit-project", project_type: "observe" }),
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    new FIGenkitSpanProcessor({ tracerProvider: provider }).onEnd(
      v1Span({ name: "fixture/mock", attributes: modelAttributes(), spanId: "5555555555555557" }),
    );
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    return exporter.getFinishedSpans()[0].attributes;
  }

  it("exports the prompt by default", () => {
    expect(exportedAttributes()["input.value"]).toContain(PROMPT_MARKER);
  });

  it("FI_HIDE_INPUTS=on hides the input when the span is converted", () => {
    process.env.FI_HIDE_INPUTS = "on";
    expect(exportedAttributes()["input.value"]).toBe("__REDACTED__");
  });

  it("an unrecognised value hides when the span is converted", () => {
    process.env.FI_HIDE_INPUTS = "enabled-please";
    process.env.FI_HIDE_OUTPUTS = "enabled-please";
    const attributes = exportedAttributes();
    expect(attributes["input.value"]).toBe("__REDACTED__");
    expect(JSON.stringify(attributes)).not.toContain(PROMPT_MARKER);
    for (const call of warn.mock.calls) expect(String(call[0])).not.toContain("enabled-please");
  });
});
