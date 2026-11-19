import { gzipSync } from "node:zlib";
import { build, match } from "../src/wire/core";
import { privacyFilter } from "../src/export-privacy";
import { resolveConfig } from "../src/config";
import { generateTraceConfig } from "@traceai/fi-core";
import { createFetchHook } from "../src/wire/fetch-runtime";

function exchange(url: string, request: any, response: any, type = "application/json") {
  return build(match("POST", url)!, { method: "POST", url, headers: {}, body: Buffer.from(JSON.stringify(request)) }, {
    status: 200, headers: { "content-type": type }, body: Buffer.from(type === "application/json" ? JSON.stringify(response) : response), size: 0, firstChunkSeconds: 0.25,
  })!;
}
const openai = "https://api.openai.com/v1/chat/completions";
test.each(["\r", "\r\n", "\n"])("SSE supports %j line endings without changing delta concatenation", newline => {
  const data = ["hello ", "world"].map(content => `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}${newline}${newline}`).join("");
  expect(exchange(openai, { messages: [] }, data, "text/event-stream").attributes["output.value"]).toBe("hello world");
});
test.each(["application/json", "application/x-ndjson"])("Ollama tool-only %s responses retain tool calls and output", type => {
  const response = { model: "llama", message: { role: "assistant", content: "", tool_calls: [{ function: { name: "weather", arguments: { city: "Oslo" } } }] }, done: true, done_reason: "stop" };
  const result = exchange("http://localhost:11434/api/chat", { model: "llama", messages: [] }, type === "application/json" ? response : JSON.stringify(response) + "\n", type);
  expect(result.attributes).toMatchObject({ "gen_ai.output.messages.0.message.role": "assistant", "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name": "weather", "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments": '{"city":"Oslo"}' });
  expect(JSON.parse(String(result.attributes["output.value"]))[0].name).toBe("weather");
});
test("Cohere v2 SSE accumulates tool argument fragments and final usage", () => {
  const events = [
    { type: "message-start", id: "co-id", delta: { message: { role: "assistant" } } },
    { type: "tool-call-start", index: 0, delta: { message: { tool_calls: { id: "call-1", function: { name: "weather", arguments: "" } } } } },
    ...['{"city":', '"Oslo"}'].map(arguments_ => ({ type: "tool-call-delta", index: 0, delta: { message: { tool_calls: { function: { arguments: arguments_ } } } } })),
    { type: "message-end", delta: { finish_reason: "TOOL_CALL", usage: { tokens: { input_tokens: 3, output_tokens: 2 } } } },
  ];
  const result = exchange("https://api.cohere.com/v2/chat", { messages: [] }, events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""), "text/event-stream");
  expect(result.attributes).toMatchObject({ "gen_ai.response.id": "co-id", "gen_ai.response.finish_reasons": ["TOOL_CALL"], "gen_ai.usage.total_tokens": 5, "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments": '{"city":"Oslo"}' });
});
test("Cohere SSE event names work without redundant payload type fields", () => {
  const events = 'event: message-start\ndata: {"id":"co-id"}\n\nevent: content-delta\ndata: {"delta":{"message":{"content":{"text":"answer"}}}}\n\nevent: message-end\ndata: {"delta":{"finish_reason":"COMPLETE","usage":{"tokens":{"input_tokens":2,"output_tokens":1}}}}\n\n';
  expect(exchange("https://api.cohere.com/v2/chat", { messages: [] }, events, "text/event-stream").attributes).toMatchObject({ "output.value": "answer", "gen_ai.response.id": "co-id", "gen_ai.usage.total_tokens": 3 });
});
test("Cohere v1 streaming text and stream-end usage are retained", () => {
  const events = [{ event_type: "text-generation", text: "hel" }, { event_type: "text-generation", text: "lo" }, { event_type: "stream-end", finish_reason: "COMPLETE", response: { generation_id: "v1-id", meta: { billed_units: { input_tokens: 2, output_tokens: 1 } } } }];
  expect(exchange("https://api.cohere.com/v1/chat", { message: "hi" }, events.map(e => JSON.stringify(e)).join("\n") + "\n", "application/x-ndjson").attributes).toMatchObject({ "output.value": "hello", "gen_ai.response.id": "v1-id", "gen_ai.usage.total_tokens": 3 });
});
test("Gemini streaming JSON arrays concatenate deltas but separate message parts", () => {
  const result = exchange("https://generativelanguage.googleapis.com/v1beta/models/gemini-test:streamGenerateContent", { contents: [{ parts: [{ text: "first" }, { text: "second" }] }] }, [{ candidates: [{ content: { parts: [{ text: "a" }] } }] }, { candidates: [{ content: { parts: [{ text: "b" }] } }] }]);
  expect(result.attributes).toMatchObject({ "gen_ai.request.stream": true, "gen_ai.response.time_to_first_chunk": 0.25, "gen_ai.input.messages.0.message.content": "first\nsecond", "output.value": "ab" });
});
test("Vertex streamRawPredict is a stream without SSE framing", () => {
  const result = exchange("https://us-east1-aiplatform.googleapis.com/v1/projects/p/locations/us-east1/publishers/anthropic/models/m:streamRawPredict", { messages: [] }, { content: [{ type: "text", text: "reply" }] });
  expect(result.attributes["gen_ai.request.stream"]).toBe(true);
});
test.each(["converse-stream", "invoke-with-response-stream"])("Bedrock %s is a stream even with an opaque body", endpoint => {
  const result = exchange(`https://bedrock-runtime.us-east-1.amazonaws.com/model/m/${endpoint}`, { messages: [] }, {});
  expect(result.attributes["gen_ai.request.stream"]).toBe(true);
});
test.each([
  [openai, { messages: [{ role: "user", content: [{ type: "text", text: "a" }, { type: "image_url", image_url: { url: "https://image.example" } }, { type: "text", text: "b" }] }] }, { choices: [{ message: { role: "assistant", content: [{ type: "text", text: "c" }, { type: "text", text: "d" }] } }] }],
  ["https://api.anthropic.com/v1/messages", { messages: [{ role: "user", content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] }] }, { content: [{ type: "text", text: "c" }, { type: "text", text: "d" }] }],
  ["https://api.openai.com/v1/responses", { input: [{ role: "user", content: [{ type: "input_text", text: "a" }, { type: "input_text", text: "b" }] }] }, { output: [{ type: "message", content: [{ type: "output_text", text: "c" }, { type: "output_text", text: "d" }] }] }],
  ["https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse", { messages: [{ role: "user", content: [{ text: "a" }, { text: "b" }] }] }, { output: { message: { role: "assistant", content: [{ text: "c" }, { text: "d" }] } } }],
])("text parts within one message join with newlines (%s)", (url, request, response) => {
  expect(exchange(url as string, request, response).attributes).toMatchObject({ "gen_ai.input.messages.0.message.content": "a\nb", "gen_ai.output.messages.0.message.content": "c\nd", "output.value": "c\nd" });
});
test("Anthropic stream deltas concatenate inside a block while separate text blocks join with newlines", () => {
  const events = [
    { type: "content_block_start", index: 0, content_block: { type: "text", text: "a" } },
    { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "b" } },
    { type: "content_block_start", index: 1, content_block: { type: "text", text: "c" } },
    { type: "content_block_delta", index: 1, delta: { type: "text_delta", text: "d" } },
  ];
  expect(exchange("https://api.anthropic.com/v1/messages", { messages: [] }, events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(""), "text/event-stream").attributes["output.value"]).toBe("ab\ncd");
});
test.each(["", " ", "invalid", "TRUE", " false "])("optional hideErrorDetails value %j preserves derivation/explicit choices", raw => {
  const config = resolveConfig({}, { FI_HIDE_INPUTS: "true", FI_HIDE_ERROR_DETAILS: raw });
  expect(generateTraceConfig(config.traceConfig).hideErrorDetails).toBe(raw.trim().toLowerCase() !== "false");
});
test.each(["hideInputs", "hideOutputs", "hideInputMessages", "hideOutputMessages", "hideInputText", "hideOutputText"] as const)("export-time %s masks JSON/flattened/raw content and event/status error details", flag => {
  const filter = privacyFilter({ [flag]: true });
  const prefix = /Input|Inputs/.test(flag) ? "input" : "output";
  const attributes = {
    [`gen_ai.${prefix}.messages`]: JSON.stringify([{ role: "user", content: "PRIVATE-CONTENT" }]),
    [`gen_ai.${prefix}.messages.0.message.content`]: "PRIVATE-CONTENT", [`${prefix}.value`]: "PRIVATE-CONTENT", [`raw.${prefix}`]: "PRIVATE-CONTENT",
    "exception.message": "PRIVATE-CONTENT", "exception.stacktrace": "PRIVATE-CONTENT", "exception.type": "Error",
  };
  const fake: any = { name: "library", attributes, resource: { attributes: {} }, status: { code: 2, message: "PRIVATE-CONTENT" }, events: [{ name: "exception", attributes }], links: [] };
  const result = filter.span(fake);
  expect(JSON.stringify({ attributes: result.attributes, status: result.status, events: result.events })).not.toContain("PRIVATE-CONTENT");
  expect(result.events[0].attributes?.["exception.type"]).toBe("Error");
  expect(fake.status.message).toBe("PRIVATE-CONTENT"); expect(fake.events[0].attributes).toBe(attributes);
});
test("flattened credential and audio fields follow the same unconditional scrubbing as JSON", () => {
  const clean = privacyFilter().attributes({ "http.request.header.authorization": "Bearer private-key", "gen_ai.input.messages.0.message.audio.source.bytes": "U0VDUkVU", "gen_ai.output.audio_base64": "U0VDUkVU" });
  const data = JSON.stringify(clean); expect(data).not.toContain("private-key"); expect(data).not.toContain("U0VDUkVU");
});
test("export-time privacy covers credentials, media, signed URLs and PII on every exported surface", () => {
  const secret = "provider-private-token", dirty = `configured-private-key ${secret} https://cdn.example/?token=signed-secret data:image/png;base64,U0VDUkVU user@example.test`;
  const attrs = { custom: dirty, json: JSON.stringify({ authorization_token: "private-json-credential", audio: { source: { bytes: "U0VDUkVU" } } }) };
  const fake: any = { name: dirty, attributes: attrs, resource: { attributes: attrs }, status: { code: 2, message: dirty }, events: [{ name: dirty, attributes: attrs }], links: [{ attributes: attrs }] };
  const result = privacyFilter({ piiRedaction: true }, ["configured-private-key"]).span(fake, [secret]);
  const serialized = JSON.stringify({ name: result.name, attributes: result.attributes, resource: result.resource.attributes, status: result.status, events: result.events, links: result.links });
  for (const value of [secret, "configured-private-key", "signed-secret", "U0VDUkVU", "user@example.test", "private-json-credential"]) expect(serialized).not.toContain(value);
  expect(fake.status.message).toBe(dirty);
});

test("completion at Content-Length is not rewritten by a late close error while decoding", async () => {
  const original = globalThis.fetch, text = '{"choices":[{"message":{"role":"assistant","content":"answer"}}]}', bytes = gzipSync(text), failure = new Error("late close"), spans: any[] = [];
  let pulls = 0, entered!: () => void, release!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; }), gate = new Promise<void>(resolve => { release = resolve; });
  globalThis.fetch = jest.fn(async () => new Response(new ReadableStream({ pull(c) { if (pulls++ === 0) c.enqueue(bytes); else c.error(failure); } }, { highWaterMark: 0 }), { headers: { "content-type": "application/json", "content-encoding": "gzip", "content-length": String(bytes.length) } }));
  const hook = createFetchHook(4096, span => spans.push(span), { active: () => undefined, skip: () => false, run: (_p, work) => work() }, [], async () => { entered(); await gate; return { body: Buffer.from(text) }; });
  try {
    const reader = (await fetch(openai, { method: "POST", body: '{"messages":[]}' })).body!.getReader();
    expect((await reader.read()).value).toBe(bytes); await started;
    await expect(reader.read()).rejects.toBe(failure);
    release(); await Promise.all([...hook.pending]);
    expect(spans).toHaveLength(1); expect(spans[0]).toMatchObject({ ok: true, attributes: { "output.value": "answer" } });
    expect(spans[0].attributes["traceai.wire.incomplete"]).toBeUndefined(); expect(spans[0].attributes["error.type"]).toBeUndefined();
  } finally { release(); hook.remove(); globalThis.fetch = original; }
});
test.each(["application-error", "iterator-exception", "exact-content-length", "eof"])("consumer close after %s is classified without changing its reason", async mode => {
  const original = globalThis.fetch, bytes = Buffer.from('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'), failure = new Error("application processing failed"), spans: any[] = [];
  let pulls = 0; const cancel = jest.fn();
  globalThis.fetch = jest.fn(async () => new Response(new ReadableStream({ pull(c) { if (pulls++ === 0) c.enqueue(bytes); else c.close(); }, cancel }, { highWaterMark: 0 }), { headers: { "content-type": "text/event-stream", ...(mode === "exact-content-length" ? { "content-length": String(bytes.length) } : {}) } }));
  const hook = createFetchHook(4096, span => spans.push(span), { active: () => undefined, skip: () => false, run: (_p, work) => work() });
  try {
    const response = await fetch(openai, { method: "POST", body: '{"messages":[]}' });
    if (mode === "iterator-exception") {
      try { for await (const chunk of response.body as any) { expect(chunk).toBe(bytes); throw failure; } }
      catch (error) { expect(error).toBe(failure); }
    } else {
      const reader = response.body!.getReader();
      expect((await reader.read()).value).toBe(bytes);
      if (mode === "exact-content-length") expect(spans).toHaveLength(1); // No extra read or cancel needed to recognize completion.
      if (mode === "eof") expect((await reader.read()).done).toBe(true);
      await reader.cancel(failure);
    }
    expect(spans).toHaveLength(1); expect(spans[0]).toMatchObject({ ok: true, attributes: { "output.value": "partial" } });
    expect(spans[0].attributes["traceai.wire.incomplete"]).toBe(["application-error", "iterator-exception"].includes(mode) ? true : undefined);
    expect(spans[0].attributes["error.type"]).toBeUndefined();
    if (mode !== "eof") expect(cancel).toHaveBeenCalledWith(mode === "iterator-exception" ? undefined : failure);
  } finally { hook.remove(); globalThis.fetch = original; }
});
