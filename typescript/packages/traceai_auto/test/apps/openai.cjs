const OpenAI = require('openai');
(async () => {
  const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: globalThis.fetch });
  const response = await client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] });
  console.log(response.choices[0].message.content);
  console.log(JSON.stringify(require('@traceai/auto').report()));
})().catch(e => { console.error(e); process.exitCode = 1; });
