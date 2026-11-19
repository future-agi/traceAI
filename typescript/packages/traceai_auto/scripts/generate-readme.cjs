// Keep the public coverage table tied to the registry the loader actually uses.
const { readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const root = resolve(__dirname, '..');
const integrations = JSON.parse(readFileSync(resolve(root, 'src/integrations.json'), 'utf8'));
const lines = [
  '| Configuration name | Target library | Instrumentation package | Setup |',
  '| --- | --- | --- | --- |',
  ...integrations.map(entry => {
    const mode = entry.enable ? 'Enable entry' : entry.manual ? 'Manual' : 'Import hook';
    const included = entry.name === 'openai' ? ' (included)' : '';
    return `| \`${entry.name}\` | ${entry.targets.map(target => `\`${target}\``).join(', ')} | \`${entry.package}\`${included} | ${mode} |`;
  }),
];
const file = resolve(root, 'README.md');
const before = readFileSync(file, 'utf8');
const pattern = /<!-- BEGIN GENERATED INTEGRATIONS -->[\s\S]*?<!-- END GENERATED INTEGRATIONS -->/;
if (!pattern.test(before)) throw new Error('README coverage markers are missing');
const after = before.replace(pattern, `<!-- BEGIN GENERATED INTEGRATIONS -->\n${lines.join('\n')}\n<!-- END GENERATED INTEGRATIONS -->`);
if (process.argv.includes('--check')) {
  if (before !== after) throw new Error('README coverage is stale; run scripts/generate-readme.cjs');
} else {
  writeFileSync(file, after);
}
