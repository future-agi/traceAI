// An application with no OpenTelemetry of its own, so no context manager: the calls of
// beside-ai-call.cjs without app-otel.cjs. In the same tick it starts one OpenAI call and
// one plain fetch to the same chat route. With MODE=stream the OpenAI call streams, and the
// plain fetch starts while the application reads the stream. The plain fetch has no AI
// integration of its own, so only wire capture can record it. Nothing here loads traceAI.
const OpenAI = require('openai');
function plainFetch() {
  return globalThis.fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'a plain fetch beside the call' }] }) })
    .then(response => response.json());
}
(async () => {
  // The SDK sends its request through the global fetch.
  const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: (url, init) => globalThis.fetch(url, init), maxRetries: 0 });
  if (process.env.MODE === 'stream') {
    const stream = await client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }], stream: true });
    let text = '', plain;
    for await (const chunk of stream) {
      // The plain fetch starts at the first chunk, while the stream is still open.
      plain ??= plainFetch();
      text += chunk.choices[0]?.delta?.content ?? '';
    }
    const answer = await plain;
    if (text !== 'hello world' || answer.choices[0].message.content !== 'hello world') throw new Error('Unexpected completion');
  } else {
    // The OpenAI call starts first, so its AI span is live when the plain fetch starts.
    const [completion, plain] = await Promise.all([
      client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] }),
      plainFetch(),
    ]);
    if (completion.choices[0].message.content !== 'hello world' || plain.choices[0].message.content !== 'hello world') throw new Error('Unexpected completion');
  }
  console.log(JSON.stringify({ mode: process.env.MODE === 'stream' ? 'stream' : 'same-tick' }));
})().catch(error => { console.error(error); process.exitCode = 1; });
