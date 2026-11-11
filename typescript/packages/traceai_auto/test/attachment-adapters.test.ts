import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

// A02: auto hands the ONE selected manager to the instrumentors it enables, as their `attachments` option.
// A real OpenAI SDK call (loopback only, synthetic image) must upload exactly once, from the native adapter, bound to
// the native LLM span; the wire layer must not upload it again. Runs in a child process because init is process-global.
const root = resolve(__dirname, "..");
function run(withManager: boolean) {
  const code = `
    const assert = require('node:assert/strict');
    const http = require('node:http');
    const { randomUUID } = require('node:crypto');
    const { InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
    const { AttachmentManager, createAttachmentHttpTransport } = require('@traceai/fi-core/attachments');
    const auto = require('./dist/src/index.js');
    const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
    (async () => {
      const initialized = [], uploaded = [];
      const scope = { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() };
      const expiry = () => new Date(Date.now() + 3600000).toISOString();
      const server = http.createServer(async (req, res) => {
        const chunks = []; for await (const c of req) chunks.push(c); const bytes = Buffer.concat(chunks);
        res.setHeader('content-type', 'application/json');
        if (req.url.endsWith('attachment-sessions')) {
          res.end(JSON.stringify({ schema_version: 1, scope, capture_session_id: JSON.parse(bytes).capture_session_id,
            policy: { version: 'fixture-v1', enabled: true, allowed_sources: ['Bytes', 'Blob'], allowed_modalities: ['image', 'audio'], require_media_redaction: false,
              require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false },
            limits: { max_upload_bytes: 1048576, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824, recording_duration_ms: 7200000,
              active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000,
              upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 },
            expires_at: expiry(), transfer_modes: ['authenticated_stream'] }));
        } else if (req.url.endsWith('/uploads')) {
          const body = JSON.parse(bytes); initialized.push(body); res.statusCode = 201;
          res.end(JSON.stringify({ upload_id: body.attachment_id, attachment_id: body.attachment_id, occurrence_id: body.occurrence_id, state: 'pending_upload',
            reserved_bytes: body.declared_bytes, expires_at: expiry(), transfer_mode: 'authenticated_stream', write_capability: 'synthetic-upload-only' }));
        } else if (req.url.endsWith('/content')) {
          uploaded.push(bytes); res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'pending_upload', reason: 'queued' }));
        } else if (req.url.includes('/tracer/')) {
          res.statusCode = 202; res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'validating', reason: 'binding_pending' }));
        } else {
          // The synthetic model API.
          res.end(JSON.stringify({ id: 'chatcmpl-synthetic', object: 'chat.completion', created: 1, model: 'gpt-4o',
            choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: 'synthetic' } }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
        }
      });
      await new Promise(r => server.listen(0, '127.0.0.1', r));
      const origin = 'http://127.0.0.1:' + server.address().port;
      const client = async req => {
        const body = req.body instanceof Blob ? Buffer.from(await req.body.arrayBuffer()) : req.body;
        return new Promise((resolve, reject) => {
          const out = http.request(origin + new URL(req.url).pathname, { method: req.method, headers: req.headers, signal: req.signal }, response => {
            const c = []; response.on('data', d => c.push(d)); response.on('end', () => resolve({ status: response.statusCode, headers: {}, body: Buffer.concat(c) }));
          }); out.on('error', reject); out.end(body);
        });
      };
      const manager = new AttachmentManager({ enabled: true, projectName: 'synthetic', projectType: 'observe', environment: () => ({}),
        transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic' }, client }) });
      assert.equal(await manager.ready(), true);
      const exporter = new InMemorySpanExporter();
      const options = { exporter, instrumentations: ['openai', 'wire'], configFile: '/nonexistent', apiKey: '', secretKey: '' };
      if (${withManager}) options.attachmentManager = manager;
      auto.init(options);
      assert.equal(auto.getAttachmentManager() === manager, ${withManager});
      const OpenAI = require('openai').default ?? require('openai').OpenAI;
      const sdk = new OpenAI({ apiKey: 'synthetic-provider-key', baseURL: origin + '/v1' });
      await sdk.chat.completions.create({ model: 'gpt-4o', messages: [{ role: 'user', content: [{ type: 'text', text: 'synthetic' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,' + PNG } }] }] });
      await auto.forceFlush(); await manager.flush(2000);
      const openai = auto.report().integrations.find(row => row.name === 'openai');
      assert.equal(openai.status, 'instrumented'); assert.equal(openai.media, 'provider_adapter');
      const spans = exporter.getFinishedSpans().filter(s => s.attributes['gen_ai.span.kind'] === 'LLM' || s.attributes['fi.span.kind'] === 'LLM');
      process.stdout.write(JSON.stringify({ uploads: uploaded.map(b => b.toString('base64')), parents: initialized.map(r => r.association.parent.span_id),
        purposes: initialized.map(r => r.purpose), llmSpans: spans.map(s => s.spanContext().spanId) }));
      await auto.shutdown(); await manager.shutdown(); server.closeAllConnections(); server.close();
    })().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exitCode = 1; });
  `;
  return spawnSync(process.execPath, ["-e", code], {
    cwd: root, encoding: "utf8", timeout: 30000,
    env: { ...process.env, FI_ATTACHMENTS_ENABLED: "", FI_API_KEY: "", FI_SECRET_KEY: "", FI_CONFIG_FILE: "/nonexistent", FI_AUTO_DEBUG: "false" },
  });
}
const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

test("the selected manager reaches the OpenAI instrumentor as `attachments`: one upload, bound to the native span", () => {
  const result = run(true);
  expect(result.stderr).toBe(""); expect(result.status).toBe(0);
  const out = JSON.parse(result.stdout);
  expect(out.uploads).toEqual([png]);
  expect(out.purposes).toEqual(["model_input"]);
  expect(out.llmSpans).toHaveLength(1); expect(out.parents).toEqual(out.llmSpans);
});
test("without an enabled manager (the default) the same call uploads nothing", () => {
  const result = run(false);
  expect(result.stderr).toBe(""); expect(result.status).toBe(0);
  expect(JSON.parse(result.stdout)).toMatchObject({ uploads: [] });
});
