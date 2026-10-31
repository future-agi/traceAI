// Install before entering the loopback-only test sandbox. No model credentials.
const { mkdirSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { execFileSync } = require('node:child_process');
const [directory, mode, coreTarball, semconvTarball] = process.argv.slice(2);
if (!directory || !['floor', 'current'].includes(mode) || !coreTarball || !semconvTarball) {
  throw new Error('usage: node install-tanstack-fixture.cjs DIRECTORY floor|current CORE_TGZ SEMCONV_TGZ');
}
const floor = mode === 'floor';
const sdk = floor ? '2.0.1' : '2.12.0';
const exporter = floor ? '0.202.0' : '0.223.0';
mkdirSync(directory, { recursive: true });
writeFileSync(resolve(directory, 'package.json'), JSON.stringify({
  private: true,
  type: 'module',
  dependencies: {
    '@traceai/fi-core': `file:${resolve(coreTarball)}`,
    '@traceai/fi-semantic-conventions': `file:${resolve(semconvTarball)}`,
    '@tanstack/ai': floor ? '0.69.0' : '1.0.1',
    '@tanstack/ai-openai': floor ? '0.30.0' : '1.0.1',
    '@opentelemetry/api': '1.9.1',
    '@opentelemetry/sdk-trace-base': sdk,
    '@opentelemetry/resources': sdk,
    '@opentelemetry/exporter-trace-otlp-http': exporter,
    '@opentelemetry/otlp-transformer': exporter,
    'typescript': '5.8.3',
    '@types/node': '22.15.29',
  },
  overrides: {
    '@opentelemetry/sdk-trace-base': sdk,
    '@opentelemetry/sdk-trace-node': sdk,
    '@opentelemetry/context-async-hooks': sdk,
    '@opentelemetry/core': sdk,
    '@opentelemetry/resources': sdk,
    '@opentelemetry/exporter-trace-otlp-http': exporter,
    '@opentelemetry/exporter-trace-otlp-grpc': exporter,
    ...(floor ? { '@tanstack/openai-base': '0.14.0' } : {}),
  },
}, null, 2));
// Resolve the subpath by package name, exactly as an ESM application does.
writeFileSync(resolve(directory, 'esm-import.mjs'), "export * from '@traceai/fi-core/tanstack';\n");
execFileSync('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: directory, stdio: 'inherit' });
