import { build, match, SpanResult } from "../src/wire/core";
import { createFetchHook, FetchHook } from "../src/wire/fetch-runtime";

const encoder = new TextEncoder();
const failure = { code: "server_error", message: "Synthetic provider failure" };
const usage = { input_tokens: 7, output_tokens: 2, total_tokens: 9 };
const output = [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "Partial answer" }] }];
const frame = (payload: unknown, event?: string) => `${event ? `event: ${event}\n` : ""}data: ${JSON.stringify(payload)}\n\n`;
let original: typeof fetch;
let hook: FetchHook | undefined;

beforeEach(() => { original = globalThis.fetch; });
afterEach(() => { hook?.remove(); hook = undefined; globalThis.fetch = original; });

function request(path: string, authorization?: string): RequestInit {
  const input = path === "responses" ? { input: "hello" } : path === "completions" ? { prompt: "hello" } : { messages: [{ role: "user", content: "hello" }] };
  return { method: "POST", headers: { "content-type": "application/json", ...(authorization ? { authorization } : {}) },
    body: JSON.stringify({ model: "fixture-model", ...input }) };
}

async function observe(path: string, body: string, type = "text/event-stream", status = 200, authorization?: string): Promise<SpanResult> {
  const spans: SpanResult[] = [], bytes = encoder.encode(body);
  const response = new Response(bytes, { status, headers: { "content-type": type } });
  const promise = Promise.resolve(response);
  const fake = jest.fn(() => promise);
  globalThis.fetch = fake;
  hook = createFetchHook<void>(4194304, span => spans.push(span), {
    active: () => undefined, skip: () => false, run: (_parent, fn) => fn(),
  });
  const init = request(path, authorization), url = `https://api.openai.com/v1/${path}`;
  const actualPromise = fetch(url, init);
  expect(actualPromise).toBe(promise);
  const actual = await actualPromise;
  expect(actual).toBe(response);
  expect(actual.status).toBe(status);
  const reader = actual.body!.getReader(), received: number[] = [];
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    received.push(...item.value);
  }
  await Promise.all([...hook.pending]);
  expect(new Uint8Array(received)).toEqual(bytes);
  expect(fake).toHaveBeenCalledTimes(1);
  expect(fake).toHaveBeenCalledWith(url, init);
  expect(spans).toHaveLength(1);
  return spans[0];
}

describe("T6-TS-CORE-AUTO-001", () => {
  test("Responses response.failed overrides HTTP success and retains partial output and usage", async () => {
    const body = frame({ type: "response.created", response: { id: "resp_fixture", model: "fixture-model", status: "in_progress" } }) +
      frame({ type: "response.output_text.delta", delta: "Partial answer" }) +
      frame({ type: "response.failed", response: { id: "resp_fixture", status: "failed", error: failure, output: [], usage } }, "response.failed");
    const result = await observe("responses", body);
    expect(result).toMatchObject({ ok: false, description: failure.message, attributes: {
      "error.type": "server_error", "http.response.status_code": 200,
      "gen_ai.response.id": "resp_fixture", "gen_ai.response.finish_reasons": ["failed"],
      "output.value": "Partial answer", "gen_ai.usage.input_tokens": 7,
      "gen_ai.usage.output_tokens": 2, "gen_ai.usage.total_tokens": 9,
    } });
  });

  test("Responses failed JSON overrides HTTP success without dropping output or usage", async () => {
    const result = await observe("responses", JSON.stringify({ id: "resp_fixture", status: "failed", error: failure, output, usage }), "application/json");
    expect(result).toMatchObject({ ok: false, description: failure.message, attributes: {
      "error.type": "server_error", "gen_ai.response.finish_reasons": ["failed"],
      "output.value": "Partial answer", "gen_ai.usage.total_tokens": 9,
    } });
  });

  test("compatible chat SSE errors retain preceding output and usage", async () => {
    const body = frame({ choices: [{ index: 0, delta: { role: "assistant", content: "Partial answer" } }],
      usage: { prompt_tokens: 7, completion_tokens: 2, total_tokens: 9 } }) +
      frame({ error: { type: "server_error", message: failure.message } }, "error");
    const result = await observe("chat/completions", body);
    expect(result).toMatchObject({ ok: false, description: failure.message, attributes: {
      "error.type": "server_error", "output.value": "Partial answer", "gen_ai.usage.total_tokens": 9,
    } });
  });

  test.each([
    { label: "Responses standalone error", path: "responses", body: frame({ type: "error", ...failure }), type: "text/event-stream" },
    { label: "Responses event-name-only error", path: "responses", body: frame(failure, "error"), type: "text/event-stream" },
    { label: "Responses error event with a provider type", path: "responses", body: frame({ type: "server_error", message: failure.message }, "error"), type: "text/event-stream" },
    { label: "chat error event with a provider type", path: "chat/completions", body: frame({ type: "server_error", message: failure.message }, "error"), type: "text/event-stream" },
    { label: "Responses event-name-only failure", path: "responses", body: frame({ response: { error: failure } }, "response.failed"), type: "text/event-stream" },
    { label: "completions SSE error", path: "completions", body: frame({ error: failure }), type: "text/event-stream" },
    { label: "chat JSON error", path: "chat/completions", body: JSON.stringify({ error: failure }), type: "application/json" },
  ])("$label records the provider code and message", async ({ path, body, type }) => {
    expect(await observe(path, body, type)).toMatchObject({ ok: false, description: failure.message,
      attributes: { "error.type": "server_error" } });
  });

  test.each([
    { label: "failed JSON without details", body: JSON.stringify({ status: "failed" }), type: "application/json" },
    { label: "failed SSE without a response object", body: frame({ type: "response.failed" }), type: "text/event-stream" },
    { label: "error SSE without details", body: frame({}, "error"), type: "text/event-stream" },
  ])("$label still has a non-HTTP error classification", async ({ body, type }) => {
    expect(await observe("responses", body, type)).toMatchObject({ ok: false, description: "Provider error",
      attributes: { "error.type": "provider_error" } });
  });

  test("a later completed event cannot erase an observed provider failure", async () => {
    const body = frame({ type: "response.failed", response: { status: "failed", error: failure } }) +
      frame({ type: "response.completed", response: { status: "completed", error: null, output, usage } });
    expect(await observe("responses", body)).toMatchObject({ ok: false, description: failure.message,
      attributes: { "error.type": "server_error", "output.value": "Partial answer", "gen_ai.usage.total_tokens": 9 } });
  });

  test.each(["application/json", "text/event-stream"])("provider errors use existing credential privacy for %s", async type => {
    const credential = "SYNTHETIC_PROVIDER_CREDENTIAL_NOT_REAL";
    const response = { status: "failed", error: { code: credential, message: `Synthetic failure mentioning ${credential}` } };
    const body = type === "application/json" ? JSON.stringify(response) : frame({ type: "response.failed", response });
    const result = await observe("responses", body, type, 200, `Bearer ${credential}`);
    expect(result.ok).toBe(false);
    expect(result.description).toContain("Synthetic failure mentioning");
    expect(result.description).not.toContain(credential);
    expect(result.attributes["error.type"]).toBeDefined();
    expect(JSON.stringify(result)).not.toContain(credential);
  });

  test.each([
    { label: "completed JSON", body: JSON.stringify({ status: "completed", error: null, output, usage }), type: "application/json", finish: "completed" },
    { label: "completed SSE", body: frame({ type: "response.completed", response: { status: "completed", error: null, output, usage } }), type: "text/event-stream", finish: "completed" },
    { label: "incomplete SSE", body: frame({ type: "response.incomplete", response: { status: "incomplete", error: null, output, usage, incomplete_details: { reason: "max_output_tokens" } } }), type: "text/event-stream", finish: "incomplete" },
  ])("$label remains successful", async ({ body, type, finish }) => {
    const result = await observe("responses", body, type);
    expect(result).toMatchObject({ ok: true, attributes: { "output.value": "Partial answer", "gen_ai.usage.total_tokens": 9,
      "gen_ai.response.finish_reasons": [finish] } });
    expect(result.attributes["error.type"]).toBeUndefined();
    expect(result.description).toBeUndefined();
  });

  test("chat finish reasons and error-like output are not provider failures", async () => {
    const content = JSON.stringify({ error: failure, status: "failed" });
    const result = await observe("chat/completions", frame({ choices: [{ delta: { content }, finish_reason: "content_filter" }] }));
    expect(result).toMatchObject({ ok: true, attributes: { "output.value": content, "gen_ai.response.finish_reasons": ["content_filter"] } });
    expect(result.attributes["error.type"]).toBeUndefined();
  });

  test("HTTP failures keep their existing status classification", async () => {
    expect(await observe("responses", JSON.stringify({ error: failure }), "application/json", 500)).toMatchObject({
      ok: false, description: failure.message, attributes: { "error.type": "500", "http.response.status_code": 500 },
    });
  });

  test("transport errors take precedence over captured provider errors", () => {
    const url = "https://api.openai.com/v1/responses", init = request("responses");
    const body = encoder.encode(frame({ type: "response.failed", response: { status: "failed", error: failure } }));
    const result = build(match("POST", url)!, { method: "POST", url, headers: { "content-type": "application/json" }, body: encoder.encode(init.body as string) },
      { status: 200, headers: { "content-type": "text/event-stream" }, body, size: body.length, error: new TypeError("Synthetic transport failure") });
    expect(result).toMatchObject({ ok: false, description: "Synthetic transport failure", attributes: { "error.type": "TypeError" } });
  });

  test("fetch rejection identity and behavior remain unchanged", async () => {
    const failure = new TypeError("Synthetic transport failure"), spans: SpanResult[] = [];
    const promise = Promise.reject(failure), fake = jest.fn(() => promise);
    globalThis.fetch = fake;
    hook = createFetchHook<void>(4194304, span => spans.push(span), {
      active: () => undefined, skip: () => false, run: (_parent, fn) => fn(),
    });
    const actual = fetch("https://api.openai.com/v1/responses", request("responses"));
    expect(actual).toBe(promise);
    await expect(actual).rejects.toBe(failure);
    expect(fake).toHaveBeenCalledTimes(1);
    expect(spans).toHaveLength(1);
    expect(spans[0]).toMatchObject({ ok: false, description: failure.message, attributes: { "error.type": "TypeError" } });
  });
});
