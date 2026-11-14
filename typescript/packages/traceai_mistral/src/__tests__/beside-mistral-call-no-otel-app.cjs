// Copied into a temporary application with the real Mistral SDK, no application
// OpenTelemetry and no manual instrumentors. The independent plain fetch needs
// wire capture, but the SDK's own request must not duplicate its model span.
const assert = require("node:assert/strict");
const { Mistral } = require("@mistralai/mistralai");
const { auto, contextManager } = globalThis.__mistralNoOtelTest;
const traced = process.argv[2] === "traced";
const { SDK_PROMPT, PLAIN_PROMPT } = process.env;
assert(SDK_PROMPT && PLAIN_PROMPT);

const plainFetch = () => fetch(process.env.MOCK_URL + "/v1/chat/completions", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ model: "fixture", messages: [{ role: "user", content: PLAIN_PROMPT }] }),
}).then(response => response.json());

(async () => {
  const client = new Mistral({
    apiKey: "sk-test-PLACEHOLDER",
    serverURL: process.env.MOCK_URL,
    // Send materialized JSON so wire capture can inspect the SDK request.
    // Keep SDK request construction, retries and response parsing real.
    httpClient: {
      request: async request => globalThis.fetch(request.url, {
        method: request.method,
        headers: request.headers,
        body: await request.text(),
        signal: request.signal,
      }),
    },
  });
  // The SDK call starts first; its span is still live when plainFetch starts.
  const [completion, plain] = await Promise.all([
    client.chat.complete({ model: "fixture", messages: [{ role: "user", content: SDK_PROMPT }] }),
    plainFetch(),
  ]);
  const report = traced ? auto.report() : undefined;
  if (traced) await auto.forceFlush();
  console.log(JSON.stringify({
    application: { text: completion.choices[0].message.content, plain: plain.choices[0].message.content },
    contextManager: contextManager(),
    active: report?.active ?? false,
    mistral: report?.integrations.find(item => item.name === "mistral")?.status ?? null,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (traced) await auto.shutdown();
});
