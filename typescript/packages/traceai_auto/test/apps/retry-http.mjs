import { createServer } from 'node:http';
export async function request() {
  const server = createServer((_request, response) => response.end('application response'));
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const response = await fetch(`http://127.0.0.1:${server.address().port}/retry`);
    if (await response.text() !== 'application response') throw new Error('Response changed');
  } finally {
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
}
