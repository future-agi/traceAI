// A real Portkey client and a plain fetch start in the same tick, without an
// application context manager, tracer provider, propagator, or manual instrumentor.
const assert = require('node:assert/strict');
const sdk = require('portkey-ai');
const { auto, contextManager } = globalThis.__portkeyNoOtelTest;
const traced = process.argv[2] === 'traced';
const { SDK_PROMPT, PLAIN_PROMPT } = process.env;
assert(SDK_PROMPT && PLAIN_PROMPT);

const plainFetch = () => fetch(process.env.MOCK_URL + '/v1/chat/completions', {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ model: 'fixture', messages: [{ role: 'user', content: PLAIN_PROMPT }] }),
}).then(response => response.json());

(async () => {
  // Portkey captures the global fetch when the client is constructed.
  const client = new sdk.Portkey({ apiKey: 'sk-test-PLACEHOLDER', baseURL: process.env.MOCK_URL + '/v1', maxRetries: 0 });
  // The model span is live when the caller starts the unrelated plain fetch.
  const [completion, plain] = await Promise.all([
    client.chat.completions.create({ model: 'fixture', messages: [{ role: 'user', content: SDK_PROMPT }] }),
    plainFetch(),
  ]);
  const report = traced ? auto.report() : undefined;
  if (traced) await auto.forceFlush();
  console.log(JSON.stringify({
    application: { text: completion.choices[0].message.content, plain: plain.choices[0].message.content },
    contextManager: contextManager(),
    active: report?.active ?? false,
    portkey: report?.integrations.find(item => item.name === 'portkey')?.status ?? null,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (traced) await auto.shutdown();
});
