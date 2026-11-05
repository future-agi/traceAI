const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

test('published privacy contract warns about unmasked literal kinds', () => {
  const packageRoot = path.dirname(path.dirname(require.resolve('@traceai/apollo-graphql')));
  const readme = fs.readFileSync(path.join(packageRoot, 'README.md'), 'utf8');
  assert.match(readme, /enum, Boolean and null literals remain visible/);
  assert.doesNotMatch(readme, /Literal values remain masked/);
});

test('published lifecycle contract distinguishes total and inactivity timeouts', () => {
  const packageRoot = path.dirname(path.dirname(require.resolve('@traceai/apollo-graphql')));
  const readme = fs.readFileSync(path.join(packageRoot, 'README.md'), 'utf8');
  assert.match(readme, /3-second total export deadline/);
  assert.match(readme, /destroys.*owned HTTP\(S\) agents/);
  assert.match(readme, /underlying.*socket inactivity timeout/);
  assert.match(readme, /batch processor.*4-second export timeout/);
  assert.doesNotMatch(readme, /not a wall-clock deadline/);
});

test('unsupported GraphQL versions fail before loading GraphQL or registering tracing', () => {
  // Exercise range boundaries without installing a different dependency for each
  // case. The standalone GraphQL-17 probe also runs against the real package.
  const code = `const assert = require('node:assert/strict');
    const { trace, ProxyTracerProvider } = require('@opentelemetry/api');
    const { configure } = require('@traceai/apollo-graphql');
    const metadata = require('graphql/package.json');
    const installed = metadata.version;
    const options = { apiKey: 'synthetic-key', secretKey: 'synthetic-secret', baseUrl: 'http://127.0.0.1:9' };
    for (const version of ['15.10.1', '16.10.0', '17.0.2', '16.11.0-rc.1']) {
      metadata.version = version;
      assert.throws(() => configure(options), /requires GraphQL >=16\\.11\\.0 <17/);
      assert.ok(trace.getTracerProvider() instanceof ProxyTracerProvider);
      assert.equal(require.cache[require.resolve('graphql')], undefined);
    }
    metadata.version = installed;
    configure(options).shutdown();`;
  const result = spawnSync(process.execPath, ['-e', code], {
    cwd: path.resolve(__dirname, '..'), encoding: 'utf8', timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
});
