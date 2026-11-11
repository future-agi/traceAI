const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { once } = require('node:events');
const { wrapMCPClient } = require('../dist/src');
const { harness, PNG, WAV, PDF } = require('./attachment-harness.cjs');
const sdk = name => require(path.join(process.env.MCP_SDK_ROOT, 'dist/cjs', name));

test('real MCP SDK SSE tool screenshot, audio, resource read and cancellation', async () => {
  const { Client } = sdk('client/index.js');
  const { Server } = sdk('server/index.js');
  const { SSEClientTransport } = sdk('client/sse.js');
  const { SSEServerTransport } = sdk('server/sse.js');
  const { CallToolRequestSchema, ReadResourceRequestSchema } = sdk('types.js');
  const protocol = new Server({ name: 'synthetic-media', version: '1.0.0' }, { capabilities: { tools: {}, resources: {} } });
  const response = { content: [{ type: 'image', mimeType: 'image/png', data: PNG.toString('base64') }, { type: 'audio', mimeType: 'audio/wav', data: WAV.toString('base64') }, { type: 'resource', resource: { uri: 'file:///not-opened.pdf', mimeType: 'application/pdf', blob: PDF.toString('base64') } }] };
  protocol.setRequestHandler(CallToolRequestSchema, async request => {
    if (request.params.name === 'wait') { await new Promise(resolve => setTimeout(resolve, 100)); }
    return response;
  });
  protocol.setRequestHandler(ReadResourceRequestSchema, async () => ({ contents: [{ uri: 'fixture:pdf', mimeType: 'application/pdf', blob: PDF.toString('base64') }] }));
  let transport;
  const server = http.createServer(async (req, res) => {
    if (req.method === 'GET' && req.url === '/sse') {
      transport = new SSEServerTransport('/messages', res);
      await protocol.connect(transport);
    } else if (req.method === 'POST' && req.url.startsWith('/messages')) {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      await transport.handlePostMessage(req, res, JSON.parse(Buffer.concat(chunks)));
    } else { res.writeHead(404); res.end(); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const client = new Client({ name: 'fixture-client', version: '1.0.0' });
  await client.connect(new SSEClientTransport(new URL(`http://127.0.0.1:${server.address().port}/sse`)));
  const h = await harness();
  try {
    const traced = wrapMCPClient(client, { tracerProvider: h.provider });
    assert.deepEqual(await traced.callTool({ name: 'screenshot', arguments: {} }), response);
    await traced.readResource({ uri: 'fixture:pdf' });
    await h.manager.flush();
    assert.equal(h.uploads.length, 4);
    const spans = h.exporter.getFinishedSpans();
    assert.equal(spans.length, 2);
    assert.equal(h.records.filter(r => r.association.parent.span_id === spans[0].spanContext().spanId).length, 3);
    const abort = new AbortController();
    const pending = traced.callTool({ name: 'wait', arguments: {} }, undefined, { signal: abort.signal });
    abort.abort(); await assert.rejects(pending);
    await h.manager.flush(); assert.equal(h.uploads.length, 4);
    for (const policy of [{ enabled: false }, { enabled: true, traceConfig: { hideInputImages: true } }, { enabled: true, traceConfig: { hideInputs: true } }, { enabled: true, traceConfig: { piiRedaction: true } }]) {
      h.manager.updatePolicy(policy);
      await traced.callTool({ name: 'screenshot', arguments: {} }); await h.manager.flush();
      // hideInputImages leaves typed audio/PDF allowed; the other controls deny all.
      const expected = policy.traceConfig?.hideInputImages ? 6 : policy.enabled === false ? 4 : 6;
      assert.equal(h.uploads.length, expected);
    }
  } finally {
    await client.close(); await protocol.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await h.close();
  }
});
