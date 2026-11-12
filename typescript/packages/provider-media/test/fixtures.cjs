const http = require('node:http');
const { once } = require('node:events');
const { randomUUID } = require('node:crypto');
const { AttachmentManager, createAttachmentHttpTransport } = require('@traceai/fi-core/attachments');
const { fake, identity } = require('../../fi-core/test/attachments-fixtures.cjs');
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMZkAAAAASUVORK5CYII=', 'base64');
function wav() {
  const result = Buffer.alloc(46); result.write('RIFF'); result.writeUInt32LE(38, 4); result.write('WAVEfmt ', 8);
  result.writeUInt32LE(16, 16); result.writeUInt16LE(1, 20); result.writeUInt16LE(1, 22); result.writeUInt32LE(16000, 24);
  result.writeUInt32LE(32000, 28); result.writeUInt16LE(2, 32); result.writeUInt16LE(16, 34); result.write('data', 36); result.writeUInt32LE(2, 40);
  return result;
}
async function fixture(options = {}) {
  // sessionLimits overrides F01 session limits in the loopback bootstrap; everything else configures the manager.
  const { sessionLimits, ...config } = options;
  const uploads = []; const requests = []; const uploadsById = new Map();
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const data = Buffer.concat(chunks); requests.push({ method: req.method, path: req.url });
      res.setHeader('content-type', 'application/json');
      if (req.url.endsWith('attachment-sessions')) { const session = await fake().transport.bootstrap(JSON.parse(data)); res.end(JSON.stringify(sessionLimits ? { ...session, limits: { ...session.limits, ...sessionLimits } } : session)); }
      else if (req.url.endsWith('/uploads')) {
        const input = JSON.parse(data); const upload = { ...await fake().transport.initialize(null, input), upload_id: randomUUID() };
        uploadsById.set(upload.upload_id, input); res.statusCode = 201; res.end(JSON.stringify(upload));
      } else {
        const id = req.url.split('/').at(-2);
        if (req.url.endsWith('/content')) uploads.push({ request: uploadsById.get(id), data });
        res.statusCode = req.url.endsWith('/complete') ? 202 : 200;
        res.end(JSON.stringify({ upload_id: id, state: req.url.endsWith('/complete') ? 'validating' : 'pending_upload', reason: 'queued' }));
      }
    } catch { res.statusCode = 500; res.end('{}'); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const client = async req => {
    const res = await fetch(`http://127.0.0.1:${server.address().port}${new URL(req.url).pathname}`, { method: req.method, headers: req.headers, body: req.body, signal: req.signal });
    return { status: res.status, body: new Uint8Array(await res.arrayBuffer()) };
  };
  const manager = new AttachmentManager({ enabled: true, projectName: 'fixture', projectType: 'observe', transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic' }, client }), ...config });
  await manager.ready();
  return { manager, uploads, requests, async close() { await manager.shutdown(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}
module.exports = { fixture, png, wav, identity };
