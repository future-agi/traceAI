// Prepare a packed-package install for the offline GenAI privacy tests.
const { mkdirSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { execFileSync } = require('node:child_process');
const [directory, sdk, exporter, coreTarball, semconvTarball] = process.argv.slice(2);
if (!directory || !sdk || !exporter || !coreTarball || !semconvTarball) {
  throw new Error('Usage: install-genai-fixture.cjs DIRECTORY SDK_VERSION EXPORTER_VERSION CORE_TARBALL SEMCONV_TARBALL');
}
const root = resolve(directory);
mkdirSync(root, { recursive: true });
const overrides = {};
for (const name of ['core', 'context-async-hooks', 'resources', 'sdk-trace-base', 'sdk-trace-node']) {
  overrides[`@opentelemetry/${name}`] = sdk;
}
for (const name of ['http', 'grpc']) overrides[`@opentelemetry/exporter-trace-otlp-${name}`] = exporter;
writeFileSync(resolve(root, 'package.json'), JSON.stringify({
  name: 'fi-core-genai-fixture', private: true, overrides,
  dependencies: {
    '@traceai/fi-core': `file:${resolve(coreTarball)}`,
    '@traceai/fi-semantic-conventions': `file:${resolve(semconvTarball)}`,
    '@opentelemetry/api': '1.9.0',
    '@opentelemetry/sdk-trace-base': sdk,
    '@opentelemetry/exporter-trace-otlp-http': exporter,
  },
}, null, 2));
execFileSync('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', root], { stdio: 'inherit' });
