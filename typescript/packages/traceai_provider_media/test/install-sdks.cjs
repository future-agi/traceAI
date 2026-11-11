/* Run only on an isolated qualification worker, never in the SDK or application. */
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[2];
if (!root || !path.isAbsolute(root)) throw Error('Supply an absolute isolated SDK directory');
fs.mkdirSync(root, { recursive: true });
const entries = [
  ['openai', 'openai', '4.0.0'], ['groq', 'groq-sdk', '0.3.0'],
  ['genai', '@google/genai', '1.33.0'], ['gemini', '@google/generative-ai', '0.1.0'],
  ['vertex', '@google-cloud/vertexai', '1.0.0'], ['bedrock', '@aws-sdk/client-bedrock-runtime', '3.693.0'],
  ['together', 'together-ai', '0.5.0'], ['mistral', '@mistralai/mistralai', '0.1.0'], ['portkey', 'portkey-ai', '1.0.0'],
];
const dependencies = {}, versions = {};
for (const [name, pkg, floor] of entries) {
  const latest = execFileSync('npm', ['view', pkg, 'version'], { encoding: 'utf8' }).trim();
  versions[name] = { package: pkg, floor, latest };
  dependencies[`sdk-${name}-floor`] = `npm:${pkg}@${floor}`;
  dependencies[`sdk-${name}-latest`] = `npm:${pkg}@${latest}`;
}
fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ private: true, dependencies }, null, 2));
fs.writeFileSync(path.join(root, 'versions.json'), JSON.stringify(versions, null, 2));
console.log(JSON.stringify(versions, null, 2));
execFileSync('npm', ['install', '--prefix', root, '--ignore-scripts', '--no-audit', '--no-fund'], { stdio: 'inherit' });
