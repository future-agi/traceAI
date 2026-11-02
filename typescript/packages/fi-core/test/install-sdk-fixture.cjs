// Install packed packages with one OTel stable version throughout the dependency tree.
// Run before the network-denied tests, never from inside a downloaded package.
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const [version, destination, coreTarball, semconvTarball] = process.argv.slice(2);
if (!/^2\.\d+\.\d+$/.test(version || '') || !destination || !coreTarball || !semconvTarball) {
  throw new Error('Usage: install-sdk-fixture.cjs <OTel version> <empty directory> <core.tgz> <semconv.tgz>');
}
fs.mkdirSync(destination, { recursive: true });
if (fs.readdirSync(destination).length) throw new Error('Fixture directory must be empty');
const overrides = Object.fromEntries([
  'context-async-hooks', 'core', 'resources', 'sdk-trace-base', 'sdk-trace-node',
].map(name => [`@opentelemetry/${name}`, version]));
fs.writeFileSync(path.join(destination, 'package.json'), JSON.stringify({
  name: 'fi-core-sdk-fixture', private: true,
  dependencies: {
    '@traceai/fi-core': `file:${path.resolve(coreTarball)}`,
    '@traceai/fi-semantic-conventions': `file:${path.resolve(semconvTarball)}`,
  }, overrides,
}, null, 2));
const result = spawnSync('npm', ['install', '--prefix', destination, '--ignore-scripts', '--no-audit', '--no-fund'], {
  stdio: 'inherit',
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
