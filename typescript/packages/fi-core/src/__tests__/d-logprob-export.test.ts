import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import * as runtime from "../privacy-runtime";
import { registerSecret } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";
import type { TraceConfigOptions } from "../trace/trace-config/types";

// The optional handoff only makes this file runnable on the T3 control, which
// lacks the new API. Assertions always run against the real export boundary;
// T3 must fail the safe-token positive control, not an absent-function exception.
function bind(context: object, attributes: Record<string, unknown>): void {
  const remember = (runtime as unknown as { rememberProviderResponse?: (context: object, attrs: Record<string, unknown>, format: string) => void }).rememberProviderResponse;
  remember?.(context, attributes, "openai.chat");
}
function entry(token: string): any {
  return { token, logprob: -0.2, bytes: [...token].map(c => c.charCodeAt(0)), top_logprobs: [{ token, logprob: -0.3 }] };
}
function choice(text = "A safe answer.", tokens = ["Hello."], index = 0): any {
  return { index, message: { role: "assistant", content: text }, logprobs: { content: tokens.map(entry) }, finish_reason: "stop" };
}
function response(choices = [choice()]): any { return { id: "chat-test", object: "chat.completion", choices }; }
function span(attributes: Record<string, any>, context = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 }): any {
  return { _spanContext: context, name: "chat", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "test" }, events: [], links: [] };
}
function emit(payload: unknown, options: TraceConfigOptions = {}, extras: Record<string, any> = {}): any {
  const original = span({ "raw.output": JSON.stringify(payload), ...extras });
  bind(original._spanContext, original.attributes);
  const before = JSON.stringify(original);
  const result = privacyFilter(options).span(original);
  expect(JSON.stringify(original)).toBe(before);
  expect(result.attributes["raw.output"]).toBeDefined();
  return { clean: JSON.parse(result.attributes["raw.output"] as string), exported: result };
}
function visible(): void {
  const { clean } = emit(response());
  expect(clean.choices[0].logprobs.content[0].token).toBe("Hello.");
  expect(clean.choices[0].logprobs.content[0].top_logprobs[0].token).toBe("Hello.");
  expect(clean.choices[0].logprobs.content[0].logprob).toBe(-0.2);
}

test("R-D-LOGPROB-REGISTERED: registered token values stay private while ordinary tokens survive", () => {
  visible();
  const secret = "synthetic-d-registered-private";
  registerSecret(secret);
  const { clean } = emit(response([choice("A safe answer.", [secret, "Hello."])]));
  expect(clean.choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(clean.choices[0].logprobs.content[0].top_logprobs[0].token).toBe("<redacted>");
  expect(clean.choices[0].logprobs.content[1].token).toBe("Hello.");
});

test("R-D-LOGPROB-SHAPE: credential-shaped token values are masked without registration", () => {
  visible();
  const shaped = "sk-" + "Q".repeat(24); // Constructed fixture, not a real key.
  const { clean } = emit(response([choice("A safe answer.", [shaped, "Hello."])]));
  expect(clean.choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(clean.choices[0].logprobs.content[0].top_logprobs[0].token).toBe("<redacted>");
  expect(runtime.scrubValue({ value: shaped }).value).toBe("<redacted>");
});

test("R-D-TOKEN-OUTSIDE: identical token names outside registered response paths stay credential names", () => {
  visible();
  const input = response();
  input.token = "other";
  input.choices[0].message.token = "other";
  input.choices[0].logprobs.token = "other";
  input["choices.0.logprobs.content.0.token"] = "other";
  const { clean } = emit(input);
  expect(clean.token).toBe("<redacted>");
  expect(clean.choices[0].message.token).toBe("<redacted>");
  expect(clean.choices[0].logprobs.token).toBe("<redacted>");
  expect(clean["choices.0.logprobs.content.0.token"]).toBe("<redacted>");
  expect(runtime.isCredentialName("choices.0.logprobs.content.0.token")).toBe(true);
  for (const key of ["accessKeyId", "aws_access_key_id"]) expect(runtime.scrubValue({ [key]: "other" })[key]).toBe("<redacted>");
});

test("R-D-RECONSTRUCT: non-streaming text masking drops the entire choice logprobs including bytes", () => {
  visible();
  const secret = "synthetic-d-reconstruction-private";
  registerSecret(secret);
  const input = response([choice(`before ${secret} after`, ["synthetic-", "d-", "reconstruction-", "private"]), choice("A safe answer.", ["Hello."], 1)]);
  const { clean } = emit(input);
  expect(clean.choices[0].message.content).toBe("before <redacted> after");
  expect(clean.choices[0].logprobs).toBe("<redacted>");
  expect(clean.choices[1].logprobs.content[0].token).toBe("Hello.");
  expect(JSON.stringify(clean)).not.toContain("reconstruction-");
});

test("R-D-RECONSTRUCT: buffered stream masks earlier and later blocks per index, including split secrets", () => {
  visible();
  const secret = "synthetic-d-stream-private";
  registerSecret(secret);
  const chunk = (index: number, content: string) => ({ object: "chat.completion.chunk", choices: [{ index, delta: { content }, logprobs: { content: [entry(content)] } }] });
  const input = [chunk(7, "synthetic-d-"), chunk(1, "Hello."), chunk(7, "stream-private"), chunk(7, " later"), chunk(1, " Again.")];
  const { clean } = emit(input);
  for (const i of [0, 2, 3]) {
    expect(clean[i].choices[0].logprobs).toBe("<redacted>");
    expect(clean[i].choices[0].delta.content).toBe("<redacted>");
  }
  expect(clean[1].choices[0].logprobs.content[0].token).toBe("Hello.");
  expect(clean[4].choices[0].logprobs.content[0].token).toBe(" Again.");
  const allText = clean.map((c: any) => c.choices[0].delta.content).join("");
  expect(allText).not.toContain(secret);
  const oneDelta = emit([chunk(7, " earlier"), chunk(7, secret), chunk(7, " later")]).clean;
  expect(oneDelta.every((c: any) => c.choices[0].logprobs === "<redacted>")).toBe(true);
  // State is scoped to this buffered response, not the next request's same index.
  expect(emit([chunk(7, "Hello.")]).clean[0].choices[0].logprobs.content[0].token).toBe("Hello.");
});

test("R-D-ANCHOR: tool, user, arbitrary attributes and nested JSON cannot mint provider context", () => {
  visible();
  const nested = response([choice("safe", ["inner"])]);
  const input = response();
  input.tool_output = nested;
  input.user_content = JSON.stringify(nested);
  input.choices[0].message.tool_calls = [{ function: { arguments: JSON.stringify(nested) } }];
  const { clean, exported } = emit(input, {}, {
    "custom.payload": JSON.stringify(nested),
    "input.value": JSON.stringify(nested),
    "choices.0.logprobs.content.0.token": "inner",
  });
  expect(clean.choices[0].logprobs.content[0].token).toBe("Hello.");
  expect(clean.tool_output.choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(JSON.parse(clean.user_content).choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(JSON.parse(clean.choices[0].message.tool_calls[0].function.arguments).choices[0].logprobs.content[0].token).toBe("<redacted>");
  for (const key of ["custom.payload", "input.value"]) expect(JSON.parse(exported.attributes[key]).choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(exported.attributes["choices.0.logprobs.content.0.token"]).toBe("<redacted>");
  const unbound = privacyFilter().span(span({ "raw.output": JSON.stringify(nested) }));
  expect(JSON.parse(unbound.attributes["raw.output"] as string).choices[0].logprobs.content[0].token).toBe("<redacted>");
});

test("R-D-NATIVE-ADMISSION: trusted output survives FITracer admission and final snapshot; PII closes reconstruction", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    for (const pii of [false, true]) {
      const tracer = new FITracer({ tracer: provider.getTracer("d-native"), traceConfig: { piiRedaction: pii } });
      const native = tracer.startSpan("chat");
      const body = response([choice(pii ? "person@example.test" : "A safe answer.", ["Hello."])]);
      const attrs = { "raw.output": JSON.stringify(body), "output.value": JSON.stringify(body) };
      bind(native.spanContext(), attrs);
      native.setAttributes(attrs);
      native.end();
      await provider.forceFlush();
      const finished = exporter.getFinishedSpans().at(-1)!;
      expect(finished).toBeDefined();
      const clean = privacyFilter({ piiRedaction: pii }).span(finished);
      for (const key of ["raw.output", "output.value"]) {
        const parsed = JSON.parse(clean.attributes[key] as string);
        if (pii) expect(parsed.choices[0].logprobs).toBe("<redacted>");
        else expect(parsed.choices[0].logprobs.content[0].token).toBe("Hello.");
      }
    }
  } finally { await provider.shutdown(); }
});

test("R-D-BINDING: rewritten values and non-span surfaces lose the response exemption", () => {
  visible();
  const original = span({ "raw.output": JSON.stringify(response([choice("safe", ["inner"])])) });
  bind(original._spanContext, original.attributes);
  original.attributes["raw.output"] += " ";
  original.events = [{ name: "event", time: [1, 0], attributes: { "raw.output": JSON.stringify(response([choice("safe", ["inner"])])) } }];
  original.resource.attributes = original.events[0].attributes;
  const clean = privacyFilter().span(original);
  for (const attrs of [clean.attributes, clean.events[0].attributes!, clean.resource.attributes]) {
    expect(JSON.parse(attrs["raw.output"] as string).choices[0].logprobs.content[0].token).toBe("<redacted>");
  }
});
