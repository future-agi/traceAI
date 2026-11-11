// Workspace layout guards for this shared helper. Filesystem only: no build output needed.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync, readdirSync, existsSync } = require('node:fs');
const { basename, join, relative, resolve } = require('node:path');

const here = resolve(__dirname, '..');
const packages = resolve(here, '..');
const repo = resolve(packages, '../..');
const manifest = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'));
const dir = relative(repo, here).split('\\').join('/');
const consumers = ['traceai_langchain', 'traceai_llamaindex', 'traceai_vercel', 'traceai_genkit'];

test('helper stays outside the instrumentation directory prefix used by the @traceai/auto registry', () => {
  // traceai_auto/test/config.test.ts requires every packages/traceai_* directory (other than
  // traceai_auto and coding-agent hooks) to be an auto-registry instrumentation. This package is
  // a shared helper with no instrumentor, so it must not use that prefix.
  assert.doesNotMatch(basename(here), /^traceai_/);
  const enumerated = readdirSync(packages).filter(p => p.startsWith('traceai_') && p !== 'traceai_auto'
    && existsSync(join(packages, p, 'package.json')))
    .map(p => JSON.parse(readFileSync(join(packages, p, 'package.json'), 'utf8')).name);
  assert.ok(!enumerated.includes(manifest.name), `${manifest.name} is enumerated as an instrumentation package`);
});

test('release, manifest and repository metadata point at the real directory', () => {
  const config = JSON.parse(readFileSync(join(repo, 'release-please-config.json'), 'utf8')).packages;
  const versions = JSON.parse(readFileSync(join(repo, '.release-please-manifest.json'), 'utf8'));
  assert.equal(config[dir]?.['package-name'], manifest.name);
  assert.equal(versions[dir], manifest.version);
  assert.equal(manifest.repository.directory, dir);
  for (const key of [...Object.keys(config), ...Object.keys(versions)]) {
    if (key !== dir) assert.notEqual(basename(key).replace(/^traceai_/, ''), basename(dir), `stale release key ${key}`);
  }
});

test('every consuming package references this directory in its tsconfig and lockfile link', () => {
  const lock = readFileSync(join(packages, '..', 'pnpm-lock.yaml'), 'utf8');
  assert.ok(lock.includes(`\n  packages/${basename(here)}:\n`), 'lockfile importer key');
  for (const consumer of consumers) {
    const root = join(packages, consumer);
    const deps = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).dependencies ?? {};
    assert.ok(manifest.name in deps, `${consumer} depends on ${manifest.name}`);
    const refs = (JSON.parse(readFileSync(join(root, 'tsconfig.json'), 'utf8')).references ?? []).map(r => resolve(root, r.path));
    assert.ok(refs.includes(here), `${consumer}/tsconfig.json references ${basename(here)}`);
    const importer = lock.split(`\n  packages/${consumer}:\n`)[1]?.split('\n\n')[0] ?? '';
    assert.match(importer, new RegExp(`version: link:\\.\\./${basename(here)}\\n`), `${consumer} lockfile link`);
  }
});
