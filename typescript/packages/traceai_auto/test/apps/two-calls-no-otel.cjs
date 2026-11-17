// An application with no OpenTelemetry of its own, so no context manager. In the same tick
// it starts two OpenAI calls, each with its own question, and waits for both. Nothing here
// loads traceAI.
const OpenAI = require('openai');
const FIRST = 'first question started in the same tick', SECOND = 'second question started in the same tick';
(async () => {
  // The SDK sends its requests through the global fetch.
  const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: (url, init) => globalThis.fetch(url, init), maxRetries: 0 });
  const ask = content => client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content }] });
  // Both calls start before either has sent its request.
  const answers = await Promise.all([ask(FIRST), ask(SECOND)]);
  if (answers.some(answer => answer.choices[0].message.content !== 'hello world')) throw new Error('Unexpected completion');
  console.log(JSON.stringify({ calls: answers.length }));
})().catch(error => { console.error(error); process.exitCode = 1; });
