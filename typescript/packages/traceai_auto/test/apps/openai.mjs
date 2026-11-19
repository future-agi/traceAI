import OpenAI from 'openai';
const client = new OpenAI({ apiKey: 'mock-provider-key', baseURL: process.env.MOCK_URL + '/v1', fetch: globalThis.fetch });
const response = await client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] });
console.log(response.choices[0].message.content);
const { report } = await import('@traceai/auto');
console.log(JSON.stringify(report()));
