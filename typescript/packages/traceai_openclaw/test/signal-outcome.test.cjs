const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const http = require('node:http');
const { resolveConfig } = require('@traceai/agent-core');
const { createOpenClawAdapter } = require('../dist/adapter.js');

test('launcher preserves signal exits and exports interrupted/incomplete outcomes', async t => {
  const home = await fs.mkdtemp(join(tmpdir(), 'fi-openclaw-signals-'));
  const received = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    for (const resource of JSON.parse(body).resourceSpans ?? [])
      for (const scope of resource.scopeSpans ?? []) received.push(...scope.spans);
    res.setHeader('content-type', 'application/json'); res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const originalPath = process.env.PATH;
  t.after(async () => {
    if (originalPath === undefined) delete process.env.PATH;
    else process.env.PATH = originalPath;
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await fs.rm(home, { recursive: true, force: true });
  });
  const bin = join(home, 'bin');
  await fs.mkdir(bin, { mode: 0o700 });
  await fs.mkdir(join(home, '.openclaw'), { mode: 0o700 });
  await fs.writeFile(join(home, '.openclaw', 'openclaw.json'), '{}', { mode: 0o600 });
  const config = await resolveConfig({ agent: 'openclaw', home, env: {
    FI_AGENT_ENABLED: 'true', FI_API_KEY: 'synthetic-public', FI_SECRET_KEY: 'synthetic-private',
    FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/traces`,
  } });
  const adapter = createOpenClawAdapter(async args => {
    if (args[0] === '--version') return 'OpenClaw 2026.10.5';
    const official = args[2] === 'diagnostics-otel';
    return JSON.stringify({ plugin: { enabled: true, status: 'loaded', trustedOfficialInstall: official,
      packageName: official ? '@openclaw/diagnostics-otel' : '@traceai/futureagi-openclaw',
      version: official ? '2026.10.5' : require('../package.json').version } });
  });
  const context = { config, scope: 'user' };
  await adapter.install(context);
  process.env.PATH = bin + ':' + (originalPath ?? '');
  for (const [signal, code, outcome] of [['INT', 130, 'interrupted'], ['TERM', 143, 'interrupted'], ['KILL', 137, 'incomplete']]) {
    // Test-only native-process stand-in. It exits by a real signal, so Node's
    // close event has a null code and the launcher must preserve that signal.
    await fs.writeFile(join(bin, 'openclaw'), `#!/bin/sh\nkill -${signal} $$\n`, { mode: 0o700 });
    assert.equal(await adapter.run(context, ['agent', '--local']), code);
    const batch = received.splice(0);
    assert.equal(batch.length, 1);
    assert.equal(batch[0].name, 'openclaw.prompt');
    assert.equal(batch[0].status.code, 2);
    assert.equal(batch[0].attributes.find(a => a.key === 'fi.agent.outcome').value.stringValue, outcome);
  }
});
