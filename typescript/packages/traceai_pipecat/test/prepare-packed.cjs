// Prepare a clean npm consumer; execute its tests separately with network disabled.
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const packageDir = path.join(__dirname, '..');
const [tarball, consumer] = process.argv.slice(2);
if (!tarball || !consumer || fs.existsSync(consumer)) {
  throw new Error('Usage: node test/prepare-packed.cjs <tarball> <new-consumer-directory>');
}
fs.mkdirSync(consumer, { recursive: true });
const manifest = require('../package.json');
fs.writeFileSync(path.join(consumer, 'package.json'), JSON.stringify({
  private: true,
  dependencies: { ...manifest.devDependencies, [manifest.name]: path.resolve(tarball) },
}, null, 2));
const install = spawnSync('npm', ['install', '--prefix', consumer, '--ignore-scripts', '--no-audit', '--no-fund'], { stdio: 'inherit' });
if (install.status !== 0) process.exit(install.status ?? 1);
const installed = path.join(consumer, 'node_modules', manifest.name);
// Add only test fixtures, never source/build output, to the installed tarball.
fs.cpSync(__dirname, path.join(installed, 'test'), { recursive: true });
fs.cpSync(path.join(packageDir, 'examples'), path.join(installed, 'examples'), { recursive: true });
console.log(`Run offline: node --test ${installed}/test/*.test.cjs`);
