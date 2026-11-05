const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { pathToFileURL } = require('node:url');
const requests = [];
const server = createServer((req, res) => {
  const chunks = [];
  req.on('data', chunk => chunks.push(chunk));
  req.on('end', () => {
    requests.push(Buffer.concat(chunks).toString());
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('{}');
  });
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    process.env.FI_BASE_URL = `http://127.0.0.1:${server.address().port}`;
    process.env.FI_API_KEY = 'synthetic-fi-key';
    process.env.FI_SECRET_KEY = 'synthetic-fi-secret';
    process.env.PORTKEY_API_KEY = 'synthetic-portkey-key';
    process.env.PORTKEY_VIRTUAL_KEY = 'synthetic-virtual-key';
    process.env.PORTKEY_MODEL = 'synthetic-model';
    globalThis.fetch = async () => new Response(JSON.stringify({
      id: 'synthetic', model: 'synthetic-model',
      choices: [{ message: { role: 'assistant', content: 'synthetic completion' } }],
      usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
    }), { headers: { 'content-type': 'application/json' } });
    await import(pathToFileURL(process.argv[2]).href);
    assert.equal(requests.length, 1);
    assert.match(requests[0], /Portkey Chat Completions/);
    assert(!requests[0].includes('synthetic completion'));
    assert(!requests[0].includes('Explain a rainbow'));
    console.log('README tracing passed');
  } finally { await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
