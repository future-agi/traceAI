const { test } = require("node:test");
const assert = require("node:assert/strict");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const exec = promisify(execFile);

const optional = {
  muse: ["@traceai/muse-plugin", "museAdapter"],
  gemini: ["@traceai/gemini-cli", "geminiAdapter"],
  "kiro-cli": ["@traceai/kiro-plugin", "kiroAdapter"],
  "kiro-ide": ["@traceai/kiro-plugin/ide", "kiroIdeAdapter"],
  augment: ["@traceai/augment-plugin", "augmentAdapter"],
  "augment-ide": ["@traceai/augment-plugin", "augmentIdeAdapter"],
  "devin-cascade": ["@traceai/devin-cascade", "cascadeAdapter"],
  "cursor-cloud": ["@traceai/cursor-plugin/cloud", "cursorCloudAdapter"],
};
const registry = path.resolve(__dirname, "../dist/registry.js");
const cli = path.resolve(__dirname, "../dist/cli.js");
const prelude = `
const assert = require('node:assert/strict');
const Module = require('node:module');
const optional = ${JSON.stringify(optional)};
const requests = new Set(Object.values(optional).map(([request]) => request));
const original = Module._load;
const seen = [];
let mode = 'missing';
Module._load = function(request, ...args) {
  if (!requests.has(request)) return original.call(this, request, ...args);
  seen.push(request);
  if (mode === 'present') return Object.fromEntries(Object.entries(optional)
    .filter(([, [name]]) => name === request).map(([id, [, name]]) => [name, { id }]));
  const error = new Error('synthetic optional module failure');
  error.code = mode === 'missing' ? 'MODULE_NOT_FOUND' : 'BROKEN_ADAPTER';
  throw error;
};
const { adapterRegistry, adapterRelease, loadAdapter } = require(${JSON.stringify(registry)});
assert.deepEqual(seen, [], 'registry eagerly required a held adapter');
`;

test("held adapters are lazy, absent-safe, load when installed, and preserve non-missing failures", async () => {
  await exec(process.execPath, ["-e", prelude + `
    for (const id of Object.keys(optional)) {
      mode = 'missing'; assert.equal(loadAdapter(id), undefined, id);
      mode = 'present'; assert.equal(loadAdapter(id).id, id);
      mode = 'broken'; assert.throws(() => loadAdapter(id), { code: 'BROKEN_ADAPTER' });
      assert.equal(adapterRelease(id).qualified, false, id);
    }
    assert.equal(loadAdapter('../../untrusted-project-module'), undefined);
  `], { timeout: 15000 });
});

test("missing held adapters return registry reasons before credentials, network or writes", async t => {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "fi-missing-held-"));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  let calls = 0;
  const server = http.createServer((req, res) => { calls++; req.resume(); res.end('{}'); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  await exec(process.execPath, ["-e", prelude + `
    const { main } = require(${JSON.stringify(cli)});
    (async () => {
      let output = '';
      const write = process.stdout.write;
      process.stdout.write = chunk => { output += chunk; return true; };
      try {
        for (const id of Object.keys(optional)) {
          for (const command of ['status', 'doctor', 'setup', 'enable', 'update', 'run']) {
            output = '';
            assert.equal(await main([command, id, '--json', '--configure-only']), 2, command + ' ' + id);
            assert.equal(JSON.parse(output).code, adapterRelease(id).reason, command + ' ' + id);
          }
        }
      } finally { process.stdout.write = write; }
    })().catch(error => { console.error(error); process.exitCode = 1; });
  `], {
    cwd: home, timeout: 30000,
    env: { HOME: home, PATH: '/no-host-binaries', FI_API_KEY: 'release-test-key', FI_SECRET_KEY: 'release-test-secret',
      FI_OTLP_ENDPOINT: `http://127.0.0.1:${server.address().port}/v1/traces` },
  });
  assert.equal(calls, 0);
  assert.deepEqual(await fs.readdir(home), []);
});

test("held registry metadata matches the implementation without importing it at startup", () => {
  const { adapterRelease } = require(registry);
  assert.equal(adapterRelease('gemini').hostRange, require('../../traceai_gemini_cli/dist/index.js').TESTED_HOST);
  for (const [id, module, constant] of [
    ['kiro-ide', '../../traceai_kiro_plugin/dist/ide/index.js', 'IDE_HOLD'],
    ['augment-ide', '../../traceai_augment_plugin/dist/index.js', 'IDE_HOLD'],
  ]) assert.equal(adapterRelease(id).reason, require(module)[constant]);
});
