// Synthetic fixture shaped from the 2.1.316 native OTLP export; no captured customer content.
const kv = (values) =>
  Object.entries(values).map(([key, value]) => ({
    key,
    value:
      typeof value === "boolean"
        ? { boolValue: value }
        : typeof value === "number"
          ? { intValue: String(value) }
          : { stringValue: value },
  }));
const resource = {
  attributes: kv({
    "service.name": "claude-code",
    "service.version": "2.1.316",
    "host.name": "PRIVACY_CANARY",
  }),
};
const scope = { name: "com.anthropic.claude_code.tracing", version: "1.0.0" };
function span(name = "claude_code.llm_request", overrides = {}) {
  return {
    traceId: "a".repeat(32),
    spanId: "b".repeat(16),
    parentSpanId: "c".repeat(16),
    name,
    startTimeUnixNano: "1793400000000000000",
    endTimeUnixNano: "1793400000100000000",
    attributes: kv({
      "session.id": "11111111-1111-4111-8111-111111111111",
      "gen_ai.conversation.id": "11111111-1111-4111-8111-111111111111",
      "user.id": "PRIVACY_CANARY",
      "gen_ai.request.model": "claude-opus-5-5",
      "gen_ai.provider.name": "anthropic",
      input_tokens: 10,
      output_tokens: 2,
      cache_read_tokens: 7,
      cache_creation_tokens: 3,
      success: true,
      user_prompt: "PRIVACY_CANARY",
      error: "PRIVACY_CANARY",
      tool_name_safe: "Read",
    }),
    status: { code: 0, message: "PRIVACY_CANARY" },
    events: [
      { name: "PRIVACY_CANARY", attributes: kv({ output: "PRIVACY_CANARY" }) },
    ],
    links: [],
    ...overrides,
  };
}
function envelope(spans = [span()]) {
  return { resourceSpans: [{ resource, scopeSpans: [{ scope, spans }] }] };
}
module.exports = { kv, resource, scope, span, envelope };
