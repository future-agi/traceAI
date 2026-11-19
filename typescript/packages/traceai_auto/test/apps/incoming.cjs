const { createServer } = require('node:http');
(async () => {
  const server = createServer(async (req, res) => {
    // A route with no AI call, such as a health check.
    if (req.url === '/health') { res.end('ok'); return; }
    const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'gpt-fixture', messages: [{ role: 'user', content: 'hello' }] }) });
    res.end(await response.text());
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/handler`, { headers: process.env.REMOTE_PARENT ? { traceparent: process.env.REMOTE_PARENT } : {} }); await response.text();
  if (process.env.HEALTH === 'true') await (await fetch(`http://127.0.0.1:${server.address().port}/health`)).text();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  if (process.env.PRINT_GLOBALS === 'true') {
    const { context, propagation } = require('@opentelemetry/api');
    console.log(JSON.stringify({ context: context._getContextManager().constructor.name, propagation: propagation._getGlobalPropagator().constructor.name }));
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
