const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { once } = require('node:events');
const { wrapQuery } = require('../dist/src');
const { harness, PNG } = require('../../traceai_mcp/test/attachment-harness.cjs');

function sse(res, id, blocks, reason) {
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
  const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  send('message_start', { type: 'message_start', message: { id, type: 'message', role: 'assistant', model: 'claude-opus-5-5', content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 1 } } });
  blocks.forEach((block, index) => {
    const isTool = block.type === 'tool_use';
    send('content_block_start', { type: 'content_block_start', index, content_block: isTool ? { ...block, input: {} } : { type: 'text', text: '' } });
    send('content_block_delta', { type: 'content_block_delta', index, delta: isTool ? { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } : { type: 'text_delta', text: block.text } });
    send('content_block_stop', { type: 'content_block_stop', index });
  });
  send('message_delta', { type: 'message_delta', delta: { stop_reason: reason, stop_sequence: null }, usage: { output_tokens: 8 } });
  send('message_stop', { type: 'message_stop' }); res.end();
}

test('real Agent SDK bundled CLI Read-image tool maps original media to the exact tool span', { timeout: 180000 }, async () => {
  const { query } = await import(pathToFileURL(path.join(process.env.AGENT_SDK_ROOT, 'sdk.mjs')).href);
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-media-'));
  const imagePath = path.join(work, 'synthetic.png'); fs.writeFileSync(imagePath, PNG);
  let calls = 0, mediaRequests = 0;
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    let body = {}; try { body = JSON.parse(Buffer.concat(chunks).toString() || '{}'); } catch {}
    if (req.method !== 'POST' || !req.url.startsWith('/v1/messages') || req.url.includes('count_tokens')) { res.writeHead(404); res.end('{}'); return; }
    calls++;
    const toolResult = (body.messages ?? []).some(message => Array.isArray(message.content) && message.content.some(block => block.type === 'tool_result'));
    if (toolResult) mediaRequests++;
    sse(res, `msg_${calls}`, toolResult ? [{ type: 'text', text: 'Synthetic image observed.' }] : [{ type: 'tool_use', id: `tool_${calls}`, name: 'Read', input: { file_path: imagePath } }], toolResult ? 'end_turn' : 'tool_use');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  try {
    for (const mode of ['enabled', 'disabled', 'hidden', 'pii']) {
      const h = await harness({ manager: mode === 'disabled' ? { enabled: false } : mode === 'hidden' ? { traceConfig: { hideInputs: true } } : mode === 'pii' ? { traceConfig: { piiRedaction: true } } : {} });
      const abortController = new AbortController(), timer = setTimeout(() => abortController.abort(), 35000);
      try {
        const observed = [];
        const traced = wrapQuery(query, { tracerProvider: h.provider });
        const stream = traced({ prompt: 'Read the synthetic.png fixture.', options: {
          cwd: work, model: 'claude-opus-5-5', maxTurns: 3, allowedTools: ['Read'], permissionMode: 'default', settingSources: [], persistSession: false, abortController,
          canUseTool: async (name, input) => name === 'Read' && input.file_path === imagePath ? { behavior: 'allow', updatedInput: input } : { behavior: 'deny', message: 'fixture only' },
          env: { PATH: process.env.PATH, HOME: work, CLAUDE_CONFIG_DIR: path.join(work, '.claude'), ANTHROPIC_BASE_URL: `http://127.0.0.1:${server.address().port}`, ANTHROPIC_API_KEY: 'sk-ant-synthetic-not-a-key', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1', DISABLE_ERROR_REPORTING: '1', DISABLE_AUTOUPDATER: '1', HTTPS_PROXY: 'http://127.0.0.1:9', HTTP_PROXY: 'http://127.0.0.1:9', NO_PROXY: '127.0.0.1,localhost' },
        } });
        for await (const frame of stream) observed.push(frame);
        await h.manager.flush();
        assert.ok(observed.some(m => m.type === 'result' && !m.is_error), `${mode}: SDK did not complete`);
        if (mode === 'enabled') {
          assert.equal(h.uploads.length, 1, 'real CLI must return original typed image, not only text');
          const tool = h.exporter.getFinishedSpans().find(s => s.name === 'tool.Read'); assert.ok(tool);
          assert.equal(h.records[0].association.parent.span_id, tool.spanContext().spanId);
          assert.ok(observed.some(m => m.type === 'user' && m.message?.content?.some(b => b.type === 'tool_result' && Array.isArray(b.content) && b.content.some(p => p.type === 'image'))));
        } else { assert.equal(h.uploads.length, 0); assert.equal(h.records.length, 0); }
      } finally { clearTimeout(timer); abortController.abort(); await h.close(); }
    }
    assert.ok(calls >= 8); assert.ok(mediaRequests >= 4);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); fs.rmSync(work, { recursive: true, force: true }); }
});
