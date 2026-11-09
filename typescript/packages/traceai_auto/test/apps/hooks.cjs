const { report } = require('@traceai/auto');
const OpenAI = require('openai');
(async () => {
  const client = new OpenAI({ apiKey: 'mock', baseURL: process.env.MOCK_URL + '/v1' });
  await client.chat.completions.create({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hi' }] });
  console.log(JSON.stringify({ report: report(), deepseekLoaded: Object.keys(require.cache).some(file => file.includes('/traceai_deepseek/')), openaiLoaded: Object.keys(require.cache).some(file => file.includes('/traceai_openai/')) }));
})().catch(error => { console.error(error); process.exitCode = 1; });
