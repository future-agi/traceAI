import { build, match, RequestData, ResponseData } from "../src/wire/core";
import { privacyFilter } from "../src/export-privacy";

const TTFT = "gen_ai.response.time_to_first_token";
const encoder = new TextEncoder();
const request: RequestData = {
  method: "POST", url: "https://api.openai.com/v1/chat/completions",
  headers: { "content-type": "application/json" },
  body: encoder.encode(JSON.stringify({ model: "fixture-model", stream: true,
    messages: [{ role: "system", content: "system words" }, { role: "user", content: "user words" }],
  })),
};
const matched = match(request.method, request.url)!;
const frame = (value: unknown) => `data: ${JSON.stringify(value)}\r\n\r\n`;
const role = frame({ choices: [{ index: 0, delta: { role: "assistant", content: "" } }] });
const text = frame({ choices: [{ index: 0, delta: { content: "héllo" } }] });
const usage = frame({ choices: [], usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 } });

function response(body: string): ResponseData {
  return { status: 200, headers: { "content-type": "text/event-stream" }, size: encoder.encode(body).length,
    body: encoder.encode(body), firstChunkSeconds: 0.1,
  };
}
function readable(attributes: any): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext() { return ctx; }, name: "chat fixture-model", kind: 3,
    startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes, status: { code: 1 },
    events: [], links: [], resource: { attributes: {} }, instrumentationScope: { name: "wire-test" },
  };
}

test("RED: canonical TTFT uses the first complete content event, not role/chunk/EOF", () => {
  const body = role + text + usage;
  const firstEnd = encoder.encode(role).length;
  // A split inside the multibyte character cannot complete the SSE event.
  const splitEnd = firstEnd + encoder.encode(text).indexOf(0xc3) + 1;
  const secondEnd = firstEnd + encoder.encode(text).length;
  const result = build(matched, request, { ...response(body), arrivalTimes: [
    { end: firstEnd, seconds: 0.1 }, { end: splitEnd, seconds: 0.15 },
    { end: secondEnd, seconds: 0.25 }, { end: encoder.encode(body).length, seconds: 0.9 },
  ] })!;
  // Base c622516 has the other two milestones but no canonical token attribute.
  expect(result.attributes).toMatchObject({
    [TTFT]: 0.25, "gen_ai.response.time_to_first_text": 0.25,
    "gen_ai.response.time_to_first_chunk": 0.1,
    "output.value": "héllo", "gen_ai.usage.input_tokens": 7, "gen_ai.usage.output_tokens": 3,
  });
  expect(privacyFilter().span(readable(result.attributes)).attributes[TTFT]).toBe(0.25);
});

test.each([
  { type: "chat tool", value: { choices: [{ delta: { tool_calls: [{ function: { name: "lookup" } }] } }] } },
  { type: "chat reasoning", value: { choices: [{ delta: { reasoning_content: "reason" } }] } },
  { type: "anthropic text", value: { type: "content_block_delta", delta: { type: "text_delta", text: "words" } } },
  { type: "responses audio", value: { type: "response.output_audio.delta", delta: "QUJD" } },
])("RED: canonical TTFT retains existing $type event semantics", ({ value }) => {
  const body = frame(value);
  const result = build(matched, request, { ...response(body), arrivalTimes: [{ end: encoder.encode(body).length, seconds: 0.25 }] })!;
  expect(result.attributes[TTFT]).toBe(0.25);
});

test("role, usage and truncated SSE frames do not fabricate first-token timing", () => {
  for (const body of [role + usage, text.trimEnd()]) {
    const result = build(matched, request, { ...response(body), arrivalTimes: [{ end: encoder.encode(body).length, seconds: 0.1 }] })!;
    expect(result.attributes[TTFT]).toBeUndefined();
    expect(result.attributes["gen_ai.response.time_to_first_text"]).toBeUndefined();
  }
});

test("native-pipe metadata-only capture stays unknown, without invented content, usage or TTFT", () => {
  const result = build(matched, request, {
    status: 200, headers: { "content-type": "text/event-stream" }, size: 0, unavailable: true,
  })!;
  expect(result.attributes["traceai.wire.body_unavailable"]).toBe(true);
  expect(result.attributes["output.value"]).toBeUndefined();
  expect(result.attributes["gen_ai.usage.input_tokens"]).toBeUndefined();
  expect(result.attributes["gen_ai.usage.output_tokens"]).toBeUndefined();
  expect(result.attributes[TTFT]).toBeUndefined();
  const clean = privacyFilter().span(readable(result.attributes));
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.attributes["gen_ai.request.model"]).toBe("<unknown: host-truncated>");
  expect(JSON.stringify(clean.attributes)).not.toContain("system words");
  expect(JSON.stringify(clean.attributes)).not.toContain("user words");
});

test("a new timing spelling must not bypass default host-loss withholding", () => {
  const clean = privacyFilter().span(readable({
    "gen_ai.span.kind": "LLM", "gen_ai.operation.name": "chat",
    "traceai.wire.body_unavailable": true,
    [TTFT]: 0.25, "input.value": "private input", "output.value": "private output",
  }));
  expect(clean.attributes[TTFT]).toBe("<unknown: host-truncated>");
  expect(JSON.stringify(clean.attributes)).not.toContain("private input");
  expect(JSON.stringify(clean.attributes)).not.toContain("private output");
});
