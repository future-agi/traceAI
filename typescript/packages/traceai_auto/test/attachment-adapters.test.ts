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

// Shared loopback double for the attachment control plane (synthetic credentials and scope only).
const controlServer = `
  const http = require('node:http');
  const { randomUUID } = require('node:crypto');
  const initialized = [], uploaded = [], transfers = [];
  const scope = { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() };
  const expiry = () => new Date(Date.now() + 3600000).toISOString();
  function control(req, bytes, res) {
    transfers.push(req.url); res.setHeader('content-type', 'application/json');
    if (req.url.endsWith('attachment-sessions')) {
      res.end(JSON.stringify({ schema_version: 1, scope, capture_session_id: JSON.parse(bytes).capture_session_id,
        policy: { version: 'fixture-v1', enabled: true, allowed_sources: ['Bytes', 'Blob'], allowed_modalities: ['image', 'audio', 'document'], require_media_redaction: false,
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
    } else { res.statusCode = 202; res.end(JSON.stringify({ upload_id: req.url.split('/').at(-2), state: 'validating', reason: 'binding_pending' })); }
  }
  function controlClient(origin) {
    return async req => {
      const body = req.body instanceof Blob ? Buffer.from(await req.body.arrayBuffer()) : req.body;
      return new Promise((resolve, reject) => {
        const out = http.request(origin + new URL(req.url).pathname, { method: req.method, headers: { ...req.headers, 'x-attachment-control': '1' }, signal: req.signal }, response => {
          const c = []; response.on('data', d => c.push(d)); response.on('end', () => resolve({ status: response.statusCode, headers: {}, body: Buffer.concat(c) }));
        }); out.on('error', reject); out.end(body);
      });
    };
  }
`;
function wavBase64(samples = 80) {
  const b = Buffer.alloc(44 + samples * 2); b.write("RIFF"); b.writeUInt32LE(b.length - 8, 4); b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(8000, 24);
  b.writeUInt32LE(16000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) b.writeInt16LE(Math.round(8000 * Math.sin(i / 3)), 44 + i * 2);
  return b.toString("base64");
}

// auto.init() with an ENABLED manager against allowlisted HTTPS hosts, wire only (no native instrumentor). The
// application's fetch is redirected to loopback BEFORE init, so auto's wire hook sees the real provider URLs.
function runWire(env: Record<string, string> = {}) {
  const code = `
    const assert = require('node:assert/strict');
    const { InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
    const { AttachmentManager, createAttachmentHttpTransport } = require('@traceai/fi-core/attachments');
    const auto = require('./dist/src/index.js');
    ${controlServer}
    const WAV = Buffer.from('${wavBase64()}', 'base64');
    (async () => {
      let providerCalls = 0;
      const server = http.createServer(async (req, res) => {
        const chunks = []; for await (const c of req) chunks.push(c); const bytes = Buffer.concat(chunks);
        if (req.headers['x-attachment-control']) { control(req, bytes, res); return; }
        providerCalls++;
        const path = req.url.split('?')[0];
        if (path === '/v1/audio/transcriptions') { res.setHeader('content-type', 'application/json'); res.end('{"text":"synthetic"}'); return; }
        // Speech: several spaced chunks, so incremental readers see several reads.
        res.writeHead(200, { 'content-type': 'audio/wav' });
        const size = Math.ceil(WAV.length / 4);
        for (let i = 0; i < WAV.length; i += size) { res.write(WAV.subarray(i, i + size)); await new Promise(r => setTimeout(r, 15)); }
        res.end();
      });
      await new Promise(r => server.listen(0, '127.0.0.1', r));
      const origin = 'http://127.0.0.1:' + server.address().port;
      const nativeFetch = globalThis.fetch;
      globalThis.fetch = (input, init) => { const u = new URL(input instanceof Request ? input.url : String(input)); return nativeFetch(origin + u.pathname + u.search, init); };
      const manager = new AttachmentManager({ enabled: true, projectName: 'synthetic', projectType: 'observe', environment: () => process.env,
        transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic' }, client: controlClient(origin) }) });
      const ready = await manager.ready();
      const exporter = new InMemorySpanExporter();
      auto.init({ exporter, instrumentations: ['wire'], configFile: '/nonexistent', apiKey: '', secretKey: '', attachmentManager: manager });
      assert.equal(auto.getAttachmentManager(), manager);
      // The kill switch flipped after bootstrap: a process-env manager must deny at admission.
      if (process.env.TRACEAI_TEST_KILL_SWITCH === '1') process.env.FI_ATTACHMENTS_ENABLED = 'false';
      const json = { 'content-type': 'application/json' };
      // 1. Speech the application materialized.
      const speech = await fetch('https://api.openai.com/v1/audio/speech', { method: 'POST', headers: json, body: JSON.stringify({ model: 'tts-1', input: 'synthetic', voice: 'alloy' }) });
      assert.deepEqual(Buffer.from(await speech.arrayBuffer()), WAV);
      // 2. Wire-only FormData (the OpenAI SDK transcription route on openai 5.x+).
      const form = new FormData(); form.set('model', 'whisper-1'); form.set('file', new Blob([WAV], { type: 'audio/wav' }), 'synthetic.wav');
      assert.deepEqual(await (await fetch('https://api.openai.com/v1/audio/transcriptions', { method: 'POST', body: form })).json(), { text: 'synthetic' });
      // 3. Wire-only incremental read (the Deepgram SDK speak route).
      const speak = await fetch('https://api.deepgram.com/v1/speak?model=aura-asteria-en', { method: 'POST', headers: json, body: JSON.stringify({ text: 'synthetic' }) });
      const reader = speak.body.getReader(), parts = [];
      for (;;) { const x = await reader.read(); if (x.done) break; parts.push(Buffer.from(x.value)); }
      assert.deepEqual(Buffer.concat(parts), WAV);
      await auto.forceFlush(); await manager.flush(2000);
      const report = auto.report().attachments;
      const spans = exporter.getFinishedSpans();
      process.stdout.write(JSON.stringify({ ready, state: report.state, providerCalls, uploads: uploaded.map(b => b.toString('base64')),
        initialized: initialized.map(r => ({ parent: r.association.parent.span_id, direction: r.direction, purpose: r.purpose, mime: r.mime_type, source: r.source_kind })),
        spans: spans.map(s => s.spanContext().spanId), controlCalls: transfers.filter(p => !p.endsWith('attachment-sessions')).length,
        status: manager.status() }));
      await auto.shutdown(); await manager.shutdown(); server.closeAllConnections(); server.close();
    })().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exitCode = 1; });
  `;
  // This manager reads process.env at each admission, so the switch must be UNSET (an empty value is a deny).
  const childEnv: Record<string, string | undefined> = { ...process.env, FI_API_KEY: "", FI_SECRET_KEY: "", FI_CONFIG_FILE: "/nonexistent", FI_AUTO_DEBUG: "false", ...env };
  delete childEnv.FI_ATTACHMENTS_ENABLED;
  return spawnSync(process.execPath, ["-e", code], { cwd: root, encoding: "utf8", timeout: 30000, env: childEnv as NodeJS.ProcessEnv });
}
test("auto.init + enabled manager + allowlisted hosts: speech, wire-only FormData and wire-only reads each upload once, bound to their wire span", () => {
  const result = runWire();
  expect(result.stderr).toBe(""); expect(result.status).toBe(0);
  const out = JSON.parse(result.stdout);
  expect(out).toMatchObject({ ready: true, state: "ready", providerCalls: 3 });
  expect(out.uploads).toEqual([wavBase64(), wavBase64(), wavBase64()]);
  expect(out.spans).toHaveLength(3);
  // Each upload is bound to a distinct wire span that auto actually exported.
  expect(new Set(out.initialized.map((r: any) => r.parent))).toEqual(new Set(out.spans));
  expect(out.initialized.map((r: any) => `${r.direction}/${r.purpose}/${r.mime}`).sort()).toEqual(["input/model_input/audio/wav", "output/model_output/audio/wav", "output/model_output/audio/wav"]);
  expect(out.status).toMatchObject({ openTickets: 0, openStreams: 0, ownedBytes: 0 });
});
test("the same calls with FI_ATTACHMENTS_ENABLED=false set after bootstrap send nothing (kill switch at admission)", () => {
  const result = runWire({ TRACEAI_TEST_KILL_SWITCH: "1" });
  expect(result.stderr).toBe(""); expect(result.status).toBe(0);
  const out = JSON.parse(result.stdout);
  // Provider traffic and wire spans are unchanged; no attachment control traffic beyond the bootstrap session.
  expect(out).toMatchObject({ ready: true, providerCalls: 3, uploads: [], initialized: [], controlCalls: 0 });
  expect(out.spans).toHaveLength(3);
  expect(out.status).toMatchObject({ openTickets: 0, openStreams: 0, ownedBytes: 0 });
});

// Anthropic: auto enables the INSTALLED @traceai/anthropic (resolved from the application directory, like a real app)
// and hands it the same manager. The application directory links only the SDK and the instrumentor.
function anthropicApp(): string {
  const { mkdtempSync, mkdirSync, symlinkSync, readFileSync } = require("node:fs") as typeof import("node:fs");
  const { tmpdir } = require("node:os") as typeof import("node:os");
  const { dirname, join } = require("node:path") as typeof import("node:path");
  const app = mkdtempSync(join(tmpdir(), "traceai-auto-anthropic-"));
  const packageRoot = (name: string, from: string) => {
    let dir = dirname(require.resolve(name, { paths: [from] }));
    for (;;) {
      try { if (JSON.parse(readFileSync(join(dir, "package.json"), "utf8")).name === name) return dir; } catch { /* ascend */ }
      const parent = dirname(dir); if (parent === dir) throw new Error(`Cannot locate ${name}`); dir = parent;
    }
  };
  for (const [name, target] of [["@anthropic-ai/sdk", packageRoot("@anthropic-ai/sdk", root)], ["@traceai/anthropic", resolve(root, "../traceai_anthropic")]]) {
    mkdirSync(join(app, "node_modules", dirname(name)), { recursive: true });
    symlinkSync(target, join(app, "node_modules", name), "dir");
  }
  return app;
}
function runAnthropic(app: string) {
  const fiCore = require.resolve("@traceai/fi-core/attachments", { paths: [root] });
  const sdkTrace = require.resolve("@opentelemetry/sdk-trace-base", { paths: [root] });
  const code = `
    const assert = require('node:assert/strict');
    const { InMemorySpanExporter } = require(${JSON.stringify(sdkTrace)});
    const { AttachmentManager, createAttachmentHttpTransport } = require(${JSON.stringify(fiCore)});
    const auto = require(${JSON.stringify(resolve(root, "dist/src/index.js"))});
    ${controlServer}
    const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
    (async () => {
      const server = http.createServer(async (req, res) => {
        const chunks = []; for await (const c of req) chunks.push(c); const bytes = Buffer.concat(chunks);
        if (req.headers['x-attachment-control']) { control(req, bytes, res); return; }
        res.setHeader('content-type', 'application/json');
        res.end(JSON.stringify({ id: 'msg_synthetic', type: 'message', role: 'assistant', model: 'claude-synthetic', stop_reason: 'end_turn', stop_sequence: null,
          content: [{ type: 'text', text: 'synthetic' }], usage: { input_tokens: 1, output_tokens: 1 } }));
      });
      await new Promise(r => server.listen(0, '127.0.0.1', r));
      const origin = 'http://127.0.0.1:' + server.address().port;
      const manager = new AttachmentManager({ enabled: true, projectName: 'synthetic', projectType: 'observe', environment: () => ({}),
        transport: createAttachmentHttpTransport({ endpoint: 'https://attachments.example', headers: { authorization: 'Bearer synthetic' }, client: controlClient(origin) }) });
      assert.equal(await manager.ready(), true);
      const exporter = new InMemorySpanExporter();
      auto.init({ exporter, instrumentations: ['anthropic', 'wire'], configFile: '/nonexistent', apiKey: '', secretKey: '', attachmentManager: manager });
      const Anthropic = require('@anthropic-ai/sdk').default ?? require('@anthropic-ai/sdk').Anthropic;
      const sdk = new Anthropic({ apiKey: 'synthetic-provider-key', baseURL: origin });
      await sdk.messages.create({ model: 'claude-synthetic', max_tokens: 8, messages: [{ role: 'user', content: [
        { type: 'text', text: 'synthetic' }, { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG } }] }] });
      await auto.forceFlush(); await manager.flush(2000);
      const row = auto.report().integrations.find(r => r.name === 'anthropic');
      const spans = exporter.getFinishedSpans().filter(s => s.attributes['gen_ai.span.kind'] === 'LLM' || s.attributes['fi.span.kind'] === 'LLM');
      process.stdout.write(JSON.stringify({ status: row && row.status, media: row && row.media, uploads: uploaded.map(b => b.toString('base64')),
        parents: initialized.map(r => r.association.parent.span_id), purposes: initialized.map(r => r.purpose), llmSpans: spans.map(s => s.spanContext().spanId) }));
      await auto.shutdown(); await manager.shutdown(); server.closeAllConnections(); server.close();
    })().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exitCode = 1; });
  `;
  return spawnSync(process.execPath, ["-e", code], {
    cwd: app, encoding: "utf8", timeout: 30000,
    env: { ...process.env, FI_ATTACHMENTS_ENABLED: "", FI_API_KEY: "", FI_SECRET_KEY: "", FI_CONFIG_FILE: "/nonexistent", FI_AUTO_DEBUG: "false" },
  });
}
test("anthropic: report().integrations[].media is provider_adapter, and the installed instrumentor uploads the image once, bound to its span", () => {
  const app = anthropicApp();
  try {
    const result = runAnthropic(app);
    expect(result.stderr).toBe(""); expect(result.status).toBe(0);
    const out = JSON.parse(result.stdout);
    expect(out).toMatchObject({ status: "instrumented", media: "provider_adapter", uploads: [png], purposes: ["model_input"] });
    expect(out.llmSpans).toHaveLength(1); expect(out.parents).toEqual(out.llmSpans);
  } finally { require("node:fs").rmSync(app, { recursive: true, force: true }); }
});
test("inventory reports media capture per integration, apart from span status", () => {
  const { inventory } = require("../src/libraries") as typeof import("../src/libraries");
  const { resolveConfig } = require("../src/config") as typeof import("../src/config");
  const rows = inventory(resolveConfig({ configFile: "/nonexistent" }, {}));
  const media = (name: string) => rows.find(row => row.name === name)?.media;
  expect(media("openai")).toBe("provider_adapter"); expect(media("anthropic")).toBe("provider_adapter");
  expect(rows.filter(row => row.media === "provider_adapter").map(row => row.name).sort()).toEqual(["anthropic", "openai"]);
});
