// An application with no OpenTelemetry of its own, so no context manager. It makes one
// OpenAI call whose transport makes a second OpenAI call inside the first one, before it
// sends the first call's own request. After the first call returns, it makes a third
// OpenAI call. Each call carries its own question. Nothing here loads traceAI.
const OpenAI = require('openai');
const options = { apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', maxRetries: 0 };
// The SDK sends its request through the global fetch.
const client = new OpenAI({ ...options, fetch: (url, init) => globalThis.fetch(url, init) });
const ask = content => client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content }] });
const outer = new OpenAI({ ...options, fetch: async (url, init) => {
  // Inside the outer call: another OpenAI call first, then the outer call's own request.
  const inside = await ask('a call inside the call');
  if (inside.choices[0].message.content !== 'hello world') throw new Error('Unexpected completion inside the call');
  return globalThis.fetch(url, init);
} });
(async () => {
  const first = await outer.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'the outer call' }] });
  // After the outer call has returned.
  const after = await ask('a call after the call');
  if (first.choices[0].message.content !== 'hello world' || after.choices[0].message.content !== 'hello world') throw new Error('Unexpected completion');
  console.log(JSON.stringify({ calls: 3 }));
})().catch(error => { console.error(error); process.exitCode = 1; });
