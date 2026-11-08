const { test } = require('node:test');
const { spawnSync } = require('node:child_process');
const { resolve } = require('node:path');
function check(value, id) { if (!value) throw new Error(id); }
for (const id of ['R-C1-BATCH-POLICY', 'R-C1-LAMBDA-BATCH']) {
  test(id, () => {
    const env = { ...process.env };
    for (const key of Object.keys(env)) if (/^(?:FI_|OTEL_)/.test(key) || key === 'NODE_OPTIONS') delete env[key];
    Object.assign(env, { HOME: '/nonexistent/traceai-c1', XDG_CONFIG_HOME: '/nonexistent/traceai-c1', FI_PROFILE: 'traceai-c1-no-profile' });
    const result = spawnSync(process.execPath, [resolve(__dirname, 'c1-batch-policy-client.cjs'), id], {
      cwd: resolve(__dirname, '../../traceai_auto'), env, encoding: 'utf8', timeout: 20000,
    });
    check(result.status === 0 && !result.error && result.stdout === '' && result.stderr === '', id);
  });
}
