import { SpanResult } from "../src/wire/core";
import { patchFetch } from "../src/wire/fetch";

const usageMetadata = { promptTokenCount: 3, candidatesTokenCount: 2, totalTokenCount: 5 };

async function capture(raw: string, type = "application/json", operation = "generateContent", candidateCount = 2) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:${operation}`;
  const init = { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
    contents: [{ role: "user", parts: [{ text: "hello" }] }], generationConfig: { candidateCount },
  }) };
  const original = globalThis.fetch, spans: SpanResult[] = [];
  const response = new Response(raw, { headers: { "content-type": type } });
  const mock = jest.fn(async () => response);
  globalThis.fetch = mock;
  const hook = patchFetch(4194304, span => spans.push(span));
  try {
    const received = await fetch(url, init);
    expect(received).toBe(response);
    expect(received.status).toBe(200);
    expect(await received.text()).toBe(raw);
    await Promise.all([...hook.pending]);
    expect(mock).toHaveBeenCalledTimes(1);
    expect(mock).toHaveBeenCalledWith(url, init);
    expect(spans).toHaveLength(1);
    expect(spans[0].ok).toBe(true);
    expect(spans[0].attributes["traceai.wire.truncated"]).toBeUndefined();
    expect(spans[0].attributes["traceai.wire.incomplete"]).toBeUndefined();
    return spans[0].attributes;
  } finally {
    hook.remove();
    globalThis.fetch = original;
  }
}

test("T6-TS-CORE-AUTO-005/json-multiple: retain both answers without changing application bytes", async () => {
  const a = await capture(JSON.stringify({ candidates: [
    { index: 0, content: { role: "model", parts: [{ text: "First" }] }, finishReason: "STOP" },
    { index: 1, content: { role: "model", parts: [{ text: "Second" }] }, finishReason: "STOP" },
  ], usageMetadata }));
  expect(a["output.value"]).toBe("First");
  expect(a["gen_ai.output.messages.0.message.role"]).toBe("assistant");
  expect(a["gen_ai.output.messages.0.message.content"]).toBe("First");
  expect(a["gen_ai.output.messages.1.message.role"]).toBe("assistant");
  expect(a["gen_ai.output.messages.1.message.content"]).toBe("Second");
  expect(a["gen_ai.output.messages.2.message.role"]).toBeUndefined();
  expect(a["gen_ai.response.finish_reasons"]).toEqual(["STOP", "STOP"]);
  expect(a["gen_ai.usage.input_tokens"]).toBe(3);
  expect(a["gen_ai.usage.output_tokens"]).toBe(2);
  expect(a["gen_ai.usage.total_tokens"]).toBe(5);
});

test("T6-TS-CORE-AUTO-005/json-order-tools: sort sparse indices and retain a tool-only alternative", async () => {
  const a = await capture(JSON.stringify({ candidates: [
    { index: 4, content: { role: "model", parts: [{ functionCall: { id: "alternative-call", name: "lookup", args: { city: "Paris" } } }] }, finishReason: "MAX_TOKENS" },
    { index: 0, content: { role: "model", parts: [{ text: "First" }] }, finishReason: "STOP" },
  ] }));
  expect(a["output.value"]).toBe("First");
  expect(a["gen_ai.output.messages.0.message.content"]).toBe("First");
  expect(a["gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"]).toBeUndefined();
  expect(a["gen_ai.output.messages.1.message.role"]).toBe("assistant");
  expect(a["gen_ai.output.messages.1.message.content"]).toBeUndefined();
  expect(a["gen_ai.output.messages.1.message.tool_calls.0.tool_call.id"]).toBe("alternative-call");
  expect(a["gen_ai.output.messages.1.message.tool_calls.0.tool_call.function.name"]).toBe("lookup");
  expect(a["gen_ai.output.messages.1.message.tool_calls.0.tool_call.function.arguments"]).toBe('{"city":"Paris"}');
  expect(a["gen_ai.response.finish_reasons"]).toEqual(["STOP", "MAX_TOKENS"]);
});

test("T6-TS-CORE-AUTO-005/json-unindexed: fall back to array positions for omitted indices", async () => {
  const a = await capture(JSON.stringify({ candidates: [
    { content: { role: "model", parts: [{ text: "First" }] } },
    { content: { role: "model", parts: [{ text: "Second" }] } },
  ] }));
  expect(a["output.value"]).toBe("First");
  expect(a["gen_ai.output.messages.0.message.content"]).toBe("First");
  expect(a["gen_ai.output.messages.1.message.content"]).toBe("Second");
});

test.each(["sse", "json"])("T6-TS-CORE-AUTO-005/stream-%s: accumulate text, calls and terminal reasons by candidate index", async encoding => {
  const chunks = [
    { candidates: [
      { index: 1, content: { role: "model", parts: [{ text: "Sec" }] } },
      { index: 0, content: { role: "model", parts: [{ text: "Fir" }] } },
    ] },
    { candidates: [{ index: 1, content: { parts: [{ text: "ond" }, { functionCall: { id: "second-call", name: "second_lookup", args: { city: "Paris" } } }] } }] },
    { candidates: [{ index: 0, content: { parts: [{ text: "st" }, { functionCall: { id: "first-call", name: "first_lookup", args: { city: "Rome" } } }] } }] },
    { candidates: [{ index: 1, finishReason: "MAX_TOKENS" }] },
    { candidates: [{ index: 0, finishReason: "STOP" }] },
    { usageMetadata },
  ];
  const raw = encoding === "sse" ? chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join("") : JSON.stringify(chunks);
  const a = await capture(raw, encoding === "sse" ? "text/event-stream" : "application/json", "streamGenerateContent");
  expect(a["gen_ai.request.stream"]).toBe(true);
  expect(a["output.value"]).toBe("First");
  expect(a["gen_ai.output.messages.0.message.content"]).toBe("First");
  expect(a["gen_ai.output.messages.1.message.content"]).toBe("Second");
  for (const [index, id, name, city] of [[0, "first-call", "first_lookup", "Rome"], [1, "second-call", "second_lookup", "Paris"]]) {
    const base = `gen_ai.output.messages.${index}.message`;
    expect(a[`${base}.role`]).toBe("assistant");
    expect(a[`${base}.tool_calls.0.tool_call.id`]).toBe(id);
    expect(a[`${base}.tool_calls.0.tool_call.function.name`]).toBe(name);
    expect(a[`${base}.tool_calls.0.tool_call.function.arguments`]).toBe(JSON.stringify({ city }));
    expect(a[`${base}.tool_calls.1.tool_call.function.name`]).toBeUndefined();
  }
  expect(a["gen_ai.output.messages.2.message.role"]).toBeUndefined();
  expect(a["gen_ai.response.finish_reasons"]).toEqual(["STOP", "MAX_TOKENS"]);
  expect(a["gen_ai.usage.total_tokens"]).toBe(5);
});

test("T6-TS-CORE-AUTO-005/single-control: preserve an unindexed single candidate", async () => {
  const a = await capture(JSON.stringify({ candidates: [
    { content: { role: "model", parts: [{ text: "First" }] }, finishReason: "STOP" },
  ] }), "application/json", "generateContent", 1);
  expect(a["output.value"]).toBe("First");
  expect(a["output.mime_type"]).toBe("text/plain");
  expect(a["gen_ai.output.messages.0.message.content"]).toBe("First");
  expect(a["gen_ai.output.messages.1.message.role"]).toBeUndefined();
  expect(a["gen_ai.response.finish_reasons"]).toEqual(["STOP"]);
});
