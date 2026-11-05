import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { generateTraceConfig, REDACTED_VALUE } from "@traceai/fi-core";
import { sanitizeSpan } from "../privacy";
import { ToolErrorHistory } from "../toolErrorHistory";

const secret = "PRIVATE_TOOL_EXCEPTION";
const aliases = ["input.value", "raw.input", "gen_ai.input.messages", "mastra.processor_run.input", "mastra.model_step.input"];
function span(input: unknown): ReadableSpan {
  const attributes = { "mastra.span.type": "processor_run",
    ...Object.fromEntries(aliases.map(key => [key, JSON.stringify(input)])) };
  return { attributes, status: { code: 2, message: secret }, events: [{
    name: "exception", attributes: { ...attributes, "exception.type": "Error", "exception.message": secret },
  }] } as unknown as ReadableSpan;
}
const user = { role: "user", content: "safe prompt", data: { error: "ordinary user field" } };
const normal = { type: "tool-result", toolCallId: "ok", toolName: "lookup", output: { type: "json", value: { error: "ordinary tool field" } } };
const error = (type: string) => ({ type: "tool-result", toolCallId: "failed", toolName: "lookup",
  output: { type, value: type === "error-json" ? { message: secret, stack: secret } : secret } });

describe("typed failed tool history", () => {
  it.each(["hideErrorDetails", "hideOutputs", "hideOutputMessages", "hideOutputText"] as const)(
    "%s masks only failed tool payloads in every span/event input alias", flag => {
      const prompt = [user, { role: "tool", content: [normal, error("error-text"), error("error-json"),
        { type: "tool-result", toolCallId: "old-error", isError: true, result: secret }] }];
      const original = span({ prompt, stepNumber: 1 });
      const before = JSON.stringify(original);
      const sanitized = sanitizeSpan(original, generateTraceConfig({ [flag]: true }));
      for (const attributes of [sanitized.attributes, sanitized.events[0].attributes!]) {
        for (const key of aliases) {
          const input = JSON.parse(attributes[key] as string);
          expect(input.prompt[0]).toEqual(user);
          expect(input.prompt[1].content).toEqual([normal,
            { ...error("error-text"), output: { type: "error-text", value: REDACTED_VALUE } },
            { ...error("error-json"), output: { type: "error-json", value: REDACTED_VALUE } },
            { type: "tool-result", toolCallId: "old-error", isError: true, result: REDACTED_VALUE }]);
          expect(input.stepNumber).toBe(1);
        }
      }
      expect(JSON.stringify(sanitized)).not.toContain(secret);
      expect(sanitized.status).toEqual({ code: 2 });
      expect(sanitized.events[0].attributes?.["exception.type"]).toBe("Error");
      expect(JSON.stringify(original)).toBe(before);
    },
  );

  it.each(["hideOutputs", "hideOutputMessages", "hideOutputText"] as const)(
    "explicit false retains typed failures with %s enabled", flag => {
      const input = { prompt: [{ role: "tool", content: [error("error-json")] }] };
      const result = sanitizeSpan(span(input), generateTraceConfig({ [flag]: true, hideErrorDetails: false }));
      for (const key of aliases) expect(JSON.parse(result.attributes[key] as string)).toEqual(input);
    },
  );

  it("recognizes direct message arrays and messages envelopes, not arbitrary user-shaped objects", () => {
    const messages = [user, { role: "tool", content: [error("error-text")] }];
    for (const input of [messages, { messages }, { body: { messages } }]) {
      expect(JSON.stringify(sanitizeSpan(span(input), generateTraceConfig({ hideErrorDetails: true })).attributes)).not.toContain(secret);
    }
    const input = { data: { prompt: messages }, messages: [{ role: "user", content: [error("error-text")] }] };
    const result = sanitizeSpan(span(input), generateTraceConfig({ hideErrorDetails: true }));
    for (const key of aliases) expect(JSON.parse(result.attributes[key] as string)).toEqual(input);
  });
});

describe("legacy failed tool provenance", () => {
  it("scopes fingerprints by trace and masks only matching raw tool result content", () => {
    const history = new ToolErrorHistory();
    history.remember("trace-a", secret);
    const input = { body: { model: "safe-model", messages: [user,
      { role: "tool", tool_call_id: "failed", content: secret },
      { role: "tool", tool_call_id: "ok", content: "safe result" },
      { role: "user", content: secret }] } };
    const source = span(input);
    const before = JSON.stringify(source);
    const config = generateTraceConfig({ hideErrorDetails: true });
    const result = sanitizeSpan(source, config, message => history.matches("trace-a", message));
    for (const attributes of [result.attributes, result.events[0].attributes!]) {
      for (const key of aliases) {
        const messages = JSON.parse(attributes[key] as string).body.messages;
        expect(messages).toEqual([user, { role: "tool", tool_call_id: "failed", content: REDACTED_VALUE },
          input.body.messages[2], input.body.messages[3]]);
      }
    }
    expect(JSON.stringify(source)).toBe(before);
    const other = sanitizeSpan(source, config, message => history.matches("trace-b", message));
    expect(JSON.parse(other.attributes["input.value"] as string)).toEqual(input);
    const enabled = sanitizeSpan(source, generateTraceConfig({ hideErrorDetails: false }), message => history.matches("trace-a", message));
    expect(JSON.parse(enabled.attributes["input.value"] as string)).toEqual(input);
    history.forget("trace-a");
    expect(history.matches("trace-a", secret)).toBe(false);
  });

  it("bounds fingerprints per trace and fails closed after saturation", () => {
    const history = new ToolErrorHistory();
    for (let i = 0; i < 129; i++) history.remember("trace", `failure-${i}`);
    expect(history.matches("trace", "failure-128")).toBe(true);
    expect(history.matches("other", "safe")).toBe(false);
    history.forget("trace");
    expect(history.matches("trace", "failure-128")).toBe(false);
  });

  it("bounds abandoned traces without silently evicting error provenance", () => {
    const history = new ToolErrorHistory();
    for (let i = 0; i < 1025; i++) history.remember(`trace-${i}`, `failure-${i}`);
    expect(history.matches("trace-0", "failure-0")).toBe(true);
    expect(history.matches("trace-0", "safe")).toBe(false);
    expect(history.matches("trace-1024", "failure-1024")).toBe(true);
    history.forget("trace-0");
    history.remember("trace-1024", "later-failure");
    expect(history.matches("trace-1024", "failure-1024")).toBe(true);
    history.clear();
    expect(history.matches("trace-1024", "safe")).toBe(false);
  });
});
