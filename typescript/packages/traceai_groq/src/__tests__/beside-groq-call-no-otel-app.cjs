// Copied into a temporary application with a real `groq-sdk` package and no OpenTelemetry
// of its own: no context manager, tracer provider or propagator, and no manual
// instrumentors. In the same tick it starts one Groq call and one plain fetch to an
// OpenAI-compatible chat route. The plain fetch has no AI integration of its own, so only
// wire capture can record it.
const assert = require("node:assert/strict");
const sdk = require("groq-sdk");
const { auto, contextManager } = globalThis.__groqNoOtelTest;
const traced = process.argv[2] === "traced";
const { SDK_PROMPT, PLAIN_PROMPT } = process.env;
assert(SDK_PROMPT && PLAIN_PROMPT);

// The application's own request, with no SDK. Its answer is read to the end.
const plainFetch = () => fetch(process.env.MOCK_URL + "/v1/chat/completions", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ model: "fixture", messages: [{ role: "user", content: PLAIN_PROMPT }] }),
}).then(response => response.json());

(async () => {
  const Groq = sdk.default ?? sdk;
  // The SDK sends its request through the global fetch, to the loopback provider.
  const client = new Groq({ apiKey: "synthetic-not-a-key", baseURL: process.env.MOCK_URL, maxRetries: 0, fetch: (url, init) => globalThis.fetch(url, init) });
  // The Groq call starts first, so its model span is live when the plain fetch starts.
  const [completion, plain] = await Promise.all([
    client.chat.completions.create({ model: "fixture", messages: [{ role: "user", content: SDK_PROMPT }] }),
    plainFetch(),
  ]);
  const report = traced ? auto.report() : undefined;
  if (traced) await auto.forceFlush();
  console.log(JSON.stringify({
    application: { text: completion.choices[0].message.content, plain: plain.choices[0].message.content },
    contextManager: contextManager(),
    active: report?.active ?? false,
    groq: report?.integrations.find(item => item.name === "groq")?.status ?? null,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (traced) await auto.shutdown();
});
