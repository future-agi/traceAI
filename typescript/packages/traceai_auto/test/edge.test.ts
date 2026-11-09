import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
const { transformSync } = require("esbuild");
function isolate(fetch: typeof globalThis.fetch): any {
  const code = readFileSync(resolve(__dirname, "../dist/edge/index.mjs"), "utf8");
  expect(code).not.toMatch(/(?:from\s*|require\s*\()["']node:/);
  const context: any = { fetch, Headers, Request, Response, Blob, FormData, URL, URLSearchParams, TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, ReadableStream, performance, console };
  runInNewContext(transformSync(code, { format: "iife", globalName: "TraceAIEdge" }).code, context);
  expect(context.process).toBeUndefined(); expect(context.Buffer).toBeUndefined(); expect(context.require).toBeUndefined();
  return context;
}
test("edge bundle imports and observes fetch with no Node APIs, env or OTel dependencies", async () => {
  const original = jest.fn(async () => new Response(JSON.stringify({ model: "gpt-fixture", choices: [{ message: { role: "assistant", content: "hello" } }] }), { headers: { "content-type": "application/json" } }));
  const scope = isolate(original), results: any[] = [];
  const hook = scope.TraceAIEdge.patchFetch(4096, (result: any) => results.push(result));
  expect(scope.TraceAIEdge.patchFetch(4096, () => {})).toBe(hook);
  const response = await scope.fetch("https://api.openai.com/v1/chat/completions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "gpt-fixture", messages: [{ role: "user", content: "hi" }] }) });
  expect((await response.json()).choices[0].message.content).toBe("hello"); expect(original).toHaveBeenCalledTimes(1);
  expect(results).toHaveLength(1); expect(results[0].attributes["output.value"]).toBe("hello");
  hook.remove(); expect(scope.fetch).toBe(original);
});
test("round3 repeated removal cannot clear a newer edge singleton", async () => {
  const scope = isolate(async () => new Response('{"choices":[]}')), first = scope.TraceAIEdge.patchFetch(4096, () => {});
  first.remove(); const emit = jest.fn(), second = scope.TraceAIEdge.patchFetch(4096, emit);
  first.remove(); const third = scope.TraceAIEdge.patchFetch(4096, () => {}); expect(third).toBe(second);
  await (await scope.fetch("https://api.openai.com/v1/chat/completions", { method: "POST", body: '{"messages":[]}' })).text();
  expect(emit).toHaveBeenCalledTimes(1); second.remove();
});
test("edge observation preserves rejection identity and ignores non-AI requests", async () => {
  const failure = new Error("network failure"), scope = isolate(jest.fn(async () => { throw failure; })), results: any[] = [];
  const hook = scope.TraceAIEdge.patchFetch(4096, (r: any) => results.push(r));
  await expect(scope.fetch("https://api.openai.com/v1/chat/completions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "gpt", messages: [] }) })).rejects.toBe(failure);
  expect(results).toHaveLength(1); expect(results[0].ok).toBe(false);
  await expect(scope.fetch("https://example.com/health")).rejects.toBe(failure); expect(results).toHaveLength(1); hook.remove();
});
test("edge bundle retains streaming bytes unchanged and emits only on consumption", async () => {
  const sse = 'data: {"choices":[{"delta":{"content":"héllo"}}]}\n\ndata: [DONE]\n\n';
  const scope = isolate(async () => new Response(sse, { headers: { "content-type": "text/event-stream" } })), results: any[] = [];
  const hook = scope.TraceAIEdge.patchFetch(4096, (r: any) => results.push(r));
  const response = await scope.fetch("https://api.openai.com/v1/chat/completions", { method: "POST", body: JSON.stringify({ model: "gpt", messages: [] }) });
  expect(results).toHaveLength(0); expect(await response.text()).toBe(sse); expect(results).toHaveLength(1); expect(results[0].attributes["output.value"]).toBe("héllo"); hook.remove();
});
