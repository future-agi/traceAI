import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createTurnTracer, resolveConfig } from '@traceai/agent-core';
import { observe } from '../dist/observer.js';

const fixture = JSON.parse(await readFile(new URL('./fixtures/opencode-1.19.0-session.json', import.meta.url))).steps;
for (const flag of ['FI_HIDE_OUTPUTS', 'FI_HIDE_OUTPUT_TEXT', 'FI_HIDE_OUTPUT_MESSAGES', 'FI_HIDE_INPUTS', 'FI_HIDE_INPUT_TEXT', 'FI_HIDE_INPUT_MESSAGES', 'FI_AGENT_CAPTURE_CONTENT', 'none']) {
  test(`actual exported OTLP honors directional privacy: ${flag}`, async t => {
    const requests = [];
    const server = createServer(async (req, res) => {
      let body = ''; for await (const chunk of req) body += chunk;
      requests.push(JSON.parse(body)); res.setHeader('content-type', 'application/json'); res.end('{}');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
    const home = await mkdtemp(join(tmpdir(), 'privacy-otlp-'));
    t.after(() => rm(home, { recursive: true, force: true }));
    const config = await resolveConfig({ agent: 'opencode-v1', home, env: {
      FI_AGENT_ENABLED: 'true', FI_API_KEY: 'synthetic-public', FI_SECRET_KEY: 'synthetic-private',
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/traces`,
      ...(flag === 'none' ? {} : { [flag]: flag === 'FI_AGENT_CAPTURE_CONTENT' ? 'false' : 'true' }),
    } });
    const tracer = createTurnTracer({ config });
    const hooks = observe(config, tracer);
    for (const step of fixture) {
      if (step.event?.properties?.info?.role === 'assistant') {
        await hooks.event({ event: structuredClone(step.event) });
        const sessionID = fixture[0].event.properties.info.id;
        await hooks['experimental.chat.messages.transform']({}, { messages: [
          { info: { role: 'assistant', sessionID }, parts: [
            { type: 'text', text: 'HIDDEN_ASSISTANT_OUTPUT_7824' },
            { type: 'tool', tool: 'read', state: { status: 'completed', input: { filePath: 'README.md' }, output: 'HIDDEN_TOOL_OUTPUT_7824' } },
          ] },
          { info: { role: 'user', sessionID }, parts: [{ type: 'text', text: 'HIDDEN_SYNTHETIC_OUTPUT_7824', synthetic: true }] },
          { info: { role: 'user', sessionID }, parts: [{ type: 'text', text: 'CONTEXT_USER_INPUT_7824' }] },
        ] });
      } else if (step.event) await hooks.event({ event: structuredClone(step.event) });
      else if (step.hook !== 'dispose') await hooks[step.hook]?.(structuredClone(step.input), structuredClone(step.output));
    }
    await tracer.flush(); await hooks.dispose();
    const spans = requests.flatMap(r => r.resourceSpans ?? []).flatMap(r => r.scopeSpans ?? []).flatMap(r => r.spans ?? []);
    assert.ok(spans.length > 0);
    const all = JSON.stringify(spans);
    for (const marker of ['HIDDEN_ASSISTANT_OUTPUT_7824', 'HIDDEN_TOOL_OUTPUT_7824', 'HIDDEN_SYNTHETIC_OUTPUT_7824', 'CONTEXT_USER_INPUT_7824']) assert.equal(all.includes(marker), flag === 'none', marker);
    const attrs = spans.flatMap(s => s.attributes);
    if (config.hideOutputs) assert.ok(attrs.every(a => a.key !== 'output.value'));
    if (config.hideInputs) assert.ok(attrs.every(a => a.key !== 'input.value'));
  });
}
