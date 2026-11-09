const { createServer } = require('node:http');
(async () => {
  const server = createServer(async (_req, res) => {
    const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] }) });
    res.end(await response.text());
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/handler`, { headers: process.env.REMOTE_PARENT ? { traceparent: process.env.REMOTE_PARENT } : {} }); await response.text();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
})().catch(e => { console.error(e); process.exitCode = 1; });
