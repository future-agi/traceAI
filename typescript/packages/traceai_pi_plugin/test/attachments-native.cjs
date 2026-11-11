// Cloud-only native Pi qualification. Real host + HTTPS attachment broker + loopback model.
// Supply PI_TEST_BINARY, ATTACHMENT_TEST_KEY and NODE_EXTRA_CA_CERTS from the cloud runner.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const https = require('node:https');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { randomUUID } = require('node:crypto');
const exec = promisify(execFile);
const { fake } = require('../../fi-core/test/attachments-fixtures.cjs');
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
const attrs = span => Object.fromEntries((span.attributes ?? []).map(a => [a.key, Object.values(a.value)[0]]));
async function main() {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'pi-attachments-native-'));
  const requests = [], batches = [], uploads = [], bytes = [], completed = [];
  const broker = fake();
  const active = new Map();
  let scopeDenied = false;
  const server = https.createServer({ key: await fs.readFile(process.env.ATTACHMENT_TEST_KEY), cert: await fs.readFile(process.env.NODE_EXTRA_CA_CERTS) }, async (req, res) => {
    try {
      const chunks = []; for await (const c of req) chunks.push(c);
      const data = Buffer.concat(chunks);
      res.setHeader('content-type', 'application/json');
      if (req.url === '/tracer/v1/attachment-sessions') {
        if (scopeDenied) { res.writeHead(403); res.end('{}'); return; }
        assert.equal(req.headers['x-api-key'], 'synthetic-api');
        res.end(JSON.stringify(await broker.transport.bootstrap(JSON.parse(data)))); return;
      }
      if (req.url.endsWith('/attachments/uploads')) {
        const body = JSON.parse(data); uploads.push(body);
        const upload = { ...await broker.transport.initialize(null, body), upload_id: randomUUID() };
        active.set(upload.upload_id, upload);
        res.writeHead(201); res.end(JSON.stringify(upload)); return;
      }
      if (req.url.endsWith('/content') || req.url.endsWith('/complete')) {
        const id = req.url.split('/').at(-2), upload = active.get(id); assert.ok(upload);
        assert.equal(req.headers.authorization, `Bearer ${upload.write_capability}`);
        assert.equal(req.headers['x-api-key'], undefined);
        if (req.url.endsWith('/content')) { bytes.push(data); res.end(JSON.stringify({ upload_id: id, state: 'pending_upload' })); }
        else { completed.push(id); res.writeHead(202); res.end(JSON.stringify({ upload_id: id, state: 'validating' })); }
        return;
      }
      if (req.url === '/tracer/v1/traces') { batches.push(JSON.parse(data)); res.end('{}'); return; }
      if (req.url === '/v1/chat/completions') {
        const body = JSON.parse(data); requests.push(body);
        const call = body.messages.at(-1).role !== 'tool';
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        const send = choice => res.write(`data: ${JSON.stringify({ id: 'mock', object: 'chat.completion.chunk', created: 1, model: 'test', choices: [choice] })}\n\n`);
        send({ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null });
        send({ index: 0, delta: call ? { tool_calls: [{ index: 0, id: 'image_call', type: 'function', function: { name: 'fixture_image', arguments: '{}' } }] } : { content: 'Done' }, finish_reason: null });
        send({ index: 0, delta: {}, finish_reason: call ? 'tool_calls' : 'stop' });
        res.end('data: [DONE]\n\n'); return;
      }
      res.writeHead(404); res.end('{}');
    } catch { res.writeHead(500); res.end('{}'); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  try {
    const origin = `https://127.0.0.1:${server.address().port}`;
    const extension = path.join(home, 'fixture.mjs');
    // Startup settling is explicit in this harness; production never blocks inference on bootstrap.
    await fs.writeFile(extension, `export default function(pi){pi.on('session_start',async()=>{await new Promise(r=>setTimeout(r,500))});pi.registerTool({name:'fixture_image',label:'fixture',description:'Synthetic image',parameters:{type:'object',properties:{}},async execute(){return {content:[{type:'image',mimeType:'image/png',data:'${png}'}]}}})}`);
    const binary = process.env.PI_TEST_BINARY || path.resolve(__dirname, '../node_modules/.bin/pi');
    const version = (await exec(binary, ['--version'], { timeout: 15000 })).stdout.trim();
    const cases = [
      ['enabled', {}], ['disabled', { FI_ATTACHMENTS_ENABLED: 'false' }],
      ['hide-inputs', { FI_HIDE_INPUTS: 'true' }], ['hide-outputs', { FI_HIDE_OUTPUTS: 'true' }],
      ['hide-images', { FI_HIDE_INPUT_IMAGES: 'true' }], ['pii', { FI_PII_REDACTION: 'true' }],
      ['content-off', { FI_AGENT_CAPTURE_CONTENT: 'false' }], ['scope-denied', {}],
    ];
    for (const [name, flags] of cases) {
      const root = path.join(home, name), agent = path.join(root, 'agent');
      await fs.mkdir(agent, { recursive: true });
      await fs.writeFile(path.join(agent, 'models.json'), JSON.stringify({ providers: { mock: {
        baseUrl: origin + '/v1', api: 'openai-completions', apiKey: 'synthetic-model', models: [{
          id: 'test', name: 'test', reasoning: false, input: ['text', 'image'],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100000, maxTokens: 1024,
        }],
      } } }));
      await fs.writeFile(path.join(agent, 'settings.json'), JSON.stringify({ packages: [], compaction: { enabled: false }, retry: { enabled: false } }));
      const env = { PATH: process.env.PATH, HOME: root, NODE_EXTRA_CA_CERTS: process.env.NODE_EXTRA_CA_CERTS,
        PI_CODING_AGENT_DIR: agent, FI_AGENT_CONFIG_DIR: path.join(root, 'config'), XDG_STATE_HOME: path.join(root, 'state'),
        PI_OFFLINE: '1', PI_SKIP_VERSION_CHECK: '1', PI_TELEMETRY: '0',
        FI_AGENT_ENABLED: 'true', FI_AGENT_CAPTURE_CONTENT: 'true', FI_API_KEY: 'synthetic-api', FI_SECRET_KEY: 'synthetic-secret',
        FI_PROJECT_NAME: 'media', FI_OTLP_ENDPOINT: origin + '/tracer/v1/traces', FI_ATTACHMENTS_ENDPOINT: origin,
        FI_ATTACHMENTS_ENABLED: 'true', ...flags };
      const start = uploads.length, traceStart = batches.length, requestStart = requests.length;
      scopeDenied = name === 'scope-denied';
      const task = exec(binary, ['--offline', '--no-approve', '--mode', 'json', '-p', '--provider', 'mock', '--model', 'test',
        '--session-id', randomUUID(), '-e', path.resolve(__dirname, '../extension.mjs'), '-e', extension, 'Use fixture_image then answer Done.'],
        { cwd: root, env, timeout: 60000, maxBuffer: 4e6 });
      // Print mode also reads piped stdin; close it or the CLI waits for input.
      task.child.stdin.end();
      const result = await task;
      assert.ok(result.stdout.includes('tool_execution_end'), name + ': real tool callback');
      assert.equal(requests.length - requestStart, 2, name + ': real provider loop');
      assert.equal(uploads.length - start, name === 'enabled' ? 1 : 0, name + ': attachment admission');
      const spans = batches.slice(traceStart).flatMap(b => b.resourceSpans.flatMap(r => r.scopeSpans.flatMap(s => s.spans)));
      assert.equal(JSON.stringify(spans).includes(png), false, name + ': OTLP payload omission');
      if (name === 'enabled') {
        assert.deepEqual(bytes.at(-1), Buffer.from(png, 'base64'));
        const upload = uploads.at(-1);
        const tool = spans.find(s => attrs(s)['fi.span.kind'] === 'TOOL' && Object.values(attrs(s)).includes('image_call'));
        if (!tool) console.error(JSON.stringify(spans.map(s => ({ name: s.name, parent: s.parentSpanId, kind: attrs(s)['fi.span.kind'] }))));
        assert.ok(tool, 'real tool span');
        assert.equal(upload.association.parent.span_id, tool.spanId);
        assert.equal(upload.tool_call_id, 'image_call');
        assert.ok(attrs(tool)['fi.attachment.refs'].includes(upload.occurrence_id));
        assert.equal(completed.length, 1);
      }
      console.log(JSON.stringify({ host: 'pi', version, scenario: name, modelRequests: requests.length - requestStart, uploads: uploads.length - start, status: 'PASS' }));
    }
  } finally {
    server.closeAllConnections(); await new Promise(r => server.close(r));
    await fs.rm(home, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
