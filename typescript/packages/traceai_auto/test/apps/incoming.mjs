import { createServer } from 'node:http';
import { report, forceFlush, shutdown } from '@traceai/auto';
const server = createServer(async (_req, res) => {
  const response = await fetch(process.env.MOCK_URL + '/v1/chat/completions', { method: 'POST', body: JSON.stringify({ model: 'gpt-fixture', messages: [] }) });
  res.end(await response.text());
});
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/handler`, { headers: { traceparent: process.env.REMOTE_PARENT } });
  await response.text();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  await forceFlush();
  console.log(JSON.stringify(report().integrations.find(integration => integration.name === 'http')));
} finally { await shutdown(); }
