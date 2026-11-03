// Install first; all runtime tests run offline against these packed dependencies.
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const [version, destination, core, semconv] = process.argv.slice(2);
const installed = spawnSync(process.execPath, [path.join(__dirname, 'install-sdk-fixture.cjs'), version, destination, core, semconv], { stdio: 'inherit' });
if (installed.status !== 0) process.exit(installed.status || 1);
const result = spawnSync('npm', ['install', '--prefix', destination, '--ignore-scripts', '--no-audit', '--no-fund', '--save-exact',
  '@opentelemetry/api@1.9.0', '@opentelemetry/exporter-trace-otlp-proto@0.202.0', '@opentelemetry/otlp-transformer@0.202.0', 'typescript@5.8.3', '@types/node@22.15.29', '@types/aws-lambda@8.10.155'], { stdio: 'inherit' });
process.exitCode = result.status ?? 1;
