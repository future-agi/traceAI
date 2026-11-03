import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';

// Jest virtualizes require() and cannot exercise require-in-the-middle. Use the
// built public entry in a fresh process, with real SDK versions under their true name.
it.each(['portkey-floor', 'portkey-v1', 'portkey-v2', 'portkey-ai'])(
  'auto-instruments %s through the public entry and preserves subclass construction', alias => {
    const temp = mkdtempSync(join(tmpdir(), 'traceai-portkey-'));
    try {
      mkdirSync(join(temp, 'node_modules'));
      symlinkSync(dirname(require.resolve(`${alias}/package.json`)), join(temp, 'node_modules/portkey-ai'), 'dir');
      const fixture = resolve(__dirname, 'fixtures/automatic.cjs');
      const result = execFileSync(process.execPath, [fixture, join(temp, 'app.cjs')], { encoding: 'utf8' });
      expect(result.trim()).toBe('automatic tracing passed');
    } finally { rmSync(temp, { recursive: true, force: true }); }
  },
);
