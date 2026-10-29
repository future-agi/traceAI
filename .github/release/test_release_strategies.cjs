// Run with NODE_PATH=<release-please@17.11.2 node_modules> node this-file.cjs.
// Exercises real release-please strategies against local files with fake history.
// No token, HTTP, git writes or GitHub mutations are possible in this test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Manifest} = require('release-please/build/src/manifest.js');
const {FileNotFoundError} = require('release-please/build/src/errors/index.js');
const root = path.resolve(__dirname, '../..');
const config = JSON.parse(fs.readFileSync(path.join(root, 'release-please-config.json')));
const baseline = JSON.parse(fs.readFileSync(path.join(root, '.release-please-manifest.json')));
const featureVersion = version => {
  const [major, minor, patch] = version.split('.').map(Number);
  return major === 0 ? `${major}.${minor}.${patch + 1}` : `${major}.${minor + 1}.0`;
};
const caseExists = file => {
  let current = root;
  for (const part of file.split('/')) {
    if (!fs.existsSync(current) || !fs.readdirSync(current).includes(part)) return false;
    current = path.join(current, part);
  }
  return true;
};
const changed = ['python/fi_instrumentation/instrumentation/config.py', 'python/frameworks/openai/traceai_openai/instrumentation.py',
  'typescript/packages/traceai_openai/src/index.ts', 'java/traceai-java-core/src/Example.java',
  'go/openai/example.go', 'csharp/src/FIInstrumentation/Example.cs'];
const scm = {
  repository: {owner: 'future-agi', repo: 'traceAI', defaultBranch: 'main'},
  async getFileJson(file) { return JSON.parse(fs.readFileSync(path.join(root, file))); },
  async getFileContentsOnBranch(file) {
    try { const text = fs.readFileSync(path.join(root, file), 'utf8');
      return {content: Buffer.from(text).toString('base64'), parsedContent: text, sha: 'a'.repeat(40)};
    } catch (err) { if (err.code === 'ENOENT') throw new FileNotFoundError(file); throw err; }
  },
  async findFilesByFilenameAndRef(name, ref, prefix = '') {
    const base = path.join(root, prefix);
    return fs.readdirSync(base, {recursive: true}).filter(f => path.basename(f) === name && fs.statSync(path.join(base, f)).isFile());
  },
  async *releaseIterator() {},
  async *tagIterator() {},
  async *mergeCommitIterator() {
    yield {sha: 'f'.repeat(40), message: 'feat: synthetic offline five-language regression', files: changed};
    yield {sha: config['bootstrap-sha'], message: 'chore: baseline', files: []};
  },
};
(async () => {
  const manifest = await Manifest.fromManifest(scm, 'main');
  const prs = await manifest.buildPullRequests();
  assert.equal(prs.length, 1, 'one combined release PR, not one per language');
  const updates = new Map();
  for (const u of prs[0].updates) {
    if (!u.createIfMissing && !caseExists(u.path)) continue;
    const original = caseExists(u.path) ? fs.readFileSync(path.join(root, u.path), 'utf8') : '';
    const value = u.updater.updateContent(original);
    if (value !== undefined) updates.set(u.path, value);
  }
  const next = JSON.parse(updates.get('.release-please-manifest.json'));
  for (const p of ['python/fi_instrumentation', 'python/frameworks/openai', 'typescript/packages/traceai_openai', 'java', 'go/openai', 'csharp/src/FIInstrumentation']) {
    assert.equal(next[p], featureVersion(baseline[p]), `${p}: feature bumps patch before 1.0, minor after 1.0`);
  }
  assert(updates.get('python/frameworks/openai/traceai_openai/version.py').includes(`__version__ = "${next['python/frameworks/openai']}"`));
  assert(updates.get('python/fi_instrumentation/instrumentation/version.py').includes(`__version__ = "${next['python/fi_instrumentation']}"`));
  assert(updates.get('python/pyproject.toml').includes(`version = "${next['python/fi_instrumentation']}"`));
  assert(!updates.has('python/frameworks/anthropic/traceai_anthropic/version.py'), 'core release must not rewrite untouched sibling SDK runtime versions');
  assert.equal(JSON.parse(updates.get('typescript/packages/traceai_openai/package.json')).version, next['typescript/packages/traceai_openai']);
  assert(updates.get('csharp/src/FIInstrumentation/FIInstrumentation.csproj').includes(`<Version>${next['csharp/src/FIInstrumentation']}</Version>`));
  assert(!next.java.includes('SNAPSHOT'), 'one release PR, no Maven snapshot PR');
  assert.match(updates.get('java/pom.xml'), new RegExp(`<version>${next.java.replaceAll('.', '\\.')}</version>`));
  const {TagName} = require('release-please/build/src/util/tag-name.js');
  const {Version} = require('release-please/build/src/version.js');
  assert.equal(new TagName(Version.parse('0.1.0'), 'go/openai', '/', true).toString(), 'go/openai/v0.1.0');
  console.log(JSON.stringify({result: 'PASS', pullRequests: prs.length, syntheticBumps: Object.fromEntries(Object.keys(next).filter(p => next[p] !== baseline[p]).map(p => [p, {from: baseline[p], to: next[p]}])), updatedFiles: [...updates.keys()]}, null, 2));
})().catch(err => { console.error(err); process.exitCode = 1; });
