// Repository-layout guards for the shared helper. They need no build and no network.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const packageDir = path.resolve(__dirname, '..');
const packagesDir = path.dirname(packageDir);
const repoRoot = path.resolve(packagesDir, '../..');
const manifest = JSON.parse(fs.readFileSync(path.join(packageDir, 'package.json'), 'utf8'));
const releasePath = path.relative(repoRoot, packageDir).split(path.sep).join('/');

test('helper lives outside the traceai_* instrumentation directories', () => {
  // `@traceai/auto` counts every packages/traceai_* directory (except auto and coding agents)
  // as an instrumentation it must register. This helper is not an instrumentation.
  assert.equal(path.basename(packagesDir), 'packages');
  assert.ok(!path.basename(packageDir).startsWith('traceai_'), `${path.basename(packageDir)} would be counted by the auto registry test`);
  const dependents = fs.readdirSync(packagesDir)
    .filter(dir => fs.existsSync(path.join(packagesDir, dir, 'package.json')))
    .filter(dir => manifest.name in (JSON.parse(fs.readFileSync(path.join(packagesDir, dir, 'package.json'), 'utf8')).dependencies ?? {}));
  assert.ok(dependents.length > 0);
  for (const dir of dependents) {
    // Packages built with project references must point at the helper's real directory.
    const tsconfig = fs.readFileSync(path.join(packagesDir, dir, 'tsconfig.json'), 'utf8');
    if (!tsconfig.includes('"references"')) continue;
    assert.ok(tsconfig.includes(`"../${path.basename(packageDir)}"`), `${dir}/tsconfig.json must reference the helper by its directory`);
  }
});

test('publishable helper has a release-please config and manifest entry', () => {
  // release_plan.py inventories every non-private package with a name and version; a missing
  // entry fails the Release workflow gates and blocks every dependent package's release.
  assert.notEqual(manifest.private, true);
  const config = JSON.parse(fs.readFileSync(path.join(repoRoot, 'release-please-config.json'), 'utf8'));
  const versions = JSON.parse(fs.readFileSync(path.join(repoRoot, '.release-please-manifest.json'), 'utf8'));
  const entry = config.packages[releasePath];
  assert.ok(entry, `release-please-config.json has no entry for ${releasePath}`);
  assert.equal(entry['release-type'], 'node');
  assert.equal(entry['package-name'], manifest.name);
  assert.equal(entry.component, `npm-${manifest.name.slice(1).replace('/', '-')}`);
  assert.equal(versions[releasePath], manifest.version);
});
