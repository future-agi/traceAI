const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

// VS Code opens a newly cloned folder in Restricted Mode. Extension test hosts always trust their
// workspace, so Restricted Mode support is asserted on the shipped VSIX itself.
const vsix = path.resolve(__dirname, '../extension.vsix');
const read = (entry) => execFileSync('unzip', ['-p', vsix, entry], { maxBuffer: 64 * 1024 * 1024 }).toString();

test('the shipped extension runs in Restricted Mode and virtual workspaces', () => {
  const manifest = JSON.parse(read('extension/package.json'));
  assert.equal(manifest.capabilities.untrustedWorkspaces.supported, true);
  assert.equal(manifest.capabilities.virtualWorkspaces.supported, true);
  assert.deepEqual(manifest.extensionKind, ['ui']);
  assert.deepEqual(manifest.activationEvents, ['onStartupFinished']);
});

test('activation does not depend on workspace trust', () => {
  const bundle = read('extension/extension.js');
  assert.doesNotMatch(bundle, /workspace\.isTrusted/);
  assert.match(bundle, /env\.remoteName/);
});
