import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

it('runs the exact README application with a synthetic SDK transport and localhost OTLP', () => {
  const root = resolve(__dirname, '../..');
  const readme = readFileSync(join(root, 'README.md'), 'utf8');
  const snippet = readme.match(/```javascript\n([\s\S]*?)\n```/)?.[1];
  expect(snippet).toBeDefined();
  const temp = mkdtempSync(join(root, '.readme-'));
  try {
    writeFileSync(join(temp, 'app.mjs'), snippet!);
    const result = execFileSync(process.execPath, [resolve(__dirname, 'fixtures/readme.cjs'), join(temp, 'app.mjs')], { encoding: 'utf8' });
    expect(result).toContain('README tracing passed');
  } finally { rmSync(temp, { recursive: true, force: true }); }
});
