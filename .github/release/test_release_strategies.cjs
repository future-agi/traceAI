// Run with NODE_PATH=<release-please@17.6.0 node_modules> node this-file.cjs.
// Exercises real release-please strategies against local files with fake history.
// No token, HTTP, git writes or GitHub mutations are possible in this test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Manifest} = require('release-please/build/src/manifest.js');
const {FileNotFoundError} = require('release-please/build/src/errors/index.js');
const root = process.argv[2] ? path.resolve(process.argv[2]) : path.resolve(__dirname, '../..');
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
  for (const p of ['python', 'python/frameworks/openai', 'typescript/packages/traceai_openai', 'java', 'go/openai', 'csharp/src/FIInstrumentation']) {
    assert.equal(next[p], featureVersion(baseline[p]), `${p}: feature bumps patch before 1.0, minor after 1.0`);
  }
  assert(updates.get('python/frameworks/openai/traceai_openai/version.py').includes(`__version__ = "${next['python/frameworks/openai']}"`));
  assert(updates.get('python/fi_instrumentation/instrumentation/version.py').includes(`__version__ = "${next['python']}"`));
  assert(updates.get('python/pyproject.toml').includes(`version = "${next['python']}"`));
  assert(!updates.has('python/frameworks/anthropic/traceai_anthropic/version.py'), 'core release must not rewrite untouched sibling SDK runtime versions');
  assert.equal(JSON.parse(updates.get('typescript/packages/traceai_openai/package.json')).version, next['typescript/packages/traceai_openai']);
  assert(updates.get('csharp/src/FIInstrumentation/FIInstrumentation.csproj').includes(`<Version>${next['csharp/src/FIInstrumentation']}</Version>`));
  assert(!next.java.includes('SNAPSHOT'), 'one release PR, no Maven snapshot PR');
  assert.match(updates.get('java/pom.xml'), new RegExp(`<version>${next.java.replaceAll('.', '\\.')}</version>`));
  // The Java lane's BOM and every literal reactor parent must move with the
  // root version. Merely checking the root POM misses a broken Maven reactor.
  const {DOMParser} = require('@xmldom/xmldom');
  const parentOf = text => new DOMParser().parseFromString(text, 'text/xml').getElementsByTagName('parent')[0];
  const valueOf = (parent, name) => parent?.getElementsByTagName(name)[0]?.textContent;
  const javaPoms = fs.readdirSync(path.join(root, 'java'), {recursive: true})
    .filter(f => path.basename(f) === 'pom.xml').map(f => `java/${f}`);
  let checkedJavaParents = 0;
  for (const file of javaPoms) {
    const original = parentOf(fs.readFileSync(path.join(root, file), 'utf8'));
    if (!['com.futureagi', 'ai.traceai'].includes(valueOf(original, 'groupId'))) continue;
    const updated = parentOf(updates.get(file) || fs.readFileSync(path.join(root, file), 'utf8'));
    assert.equal(valueOf(updated, 'version'), next.java, `${file}: parent version must follow root`);
    checkedJavaParents++;
  }
  assert(checkedJavaParents > 0, 'test must cover the Java module parents');
  const {TagName} = require('release-please/build/src/util/tag-name.js');
  const {Version} = require('release-please/build/src/version.js');
  assert.equal(new TagName(Version.parse('0.1.0'), 'go/openai', '/', true).toString(), 'go/openai/v0.1.0');
  // A core dependency-only fix must release without touching any source file.
  // Conversely a sibling-only fix must never bump core or rewrite other SDKs.
  const scenarios = [];
  for (const [files, expected] of [
    [['python/pyproject.toml'], ['python']],
    [['python/fi_instrumentation/instrumentation/config.py'], ['python']],
    [['python/frameworks/openai/pyproject.toml'], ['python/frameworks/openai']],
    [['python/frameworks/new-sdk/pyproject.toml'], []],
  ]) {
    const scenarioScm = {...scm, async *mergeCommitIterator() {
      yield {sha: 'e'.repeat(40), message: 'fix: update dependency security constraint', files};
      yield {sha: config['bootstrap-sha'], message: 'chore: baseline', files: []};
    }};
    const scenario = await Manifest.fromManifest(scenarioScm, 'main');
    const pulls = await scenario.buildPullRequests();
    assert.equal(pulls.length, expected.length ? 1 : 0, `${files}: release PR count`);
    const contents = new Map();
    for (const update of pulls.flatMap(p => p.updates)) {
      if (!update.createIfMissing && !caseExists(update.path)) continue;
      const original = caseExists(update.path) ? fs.readFileSync(path.join(root, update.path), 'utf8') : '';
      const result = update.updater.updateContent(original);
      if (result !== undefined) contents.set(update.path, result);
    }
    const versions = contents.has('.release-please-manifest.json') ? JSON.parse(contents.get('.release-please-manifest.json')) : baseline;
    assert.deepEqual(Object.keys(versions).filter(p => versions[p] !== baseline[p]), expected);
    if (expected.includes('python')) {
      const [major, minor, patch] = baseline.python.split('.').map(Number);
      assert.equal(versions.python, `${major}.${minor}.${patch + 1}`);
      assert(contents.get('python/pyproject.toml').includes(`version = "${versions.python}"`));
      assert(contents.get('python/fi_instrumentation/instrumentation/version.py').includes(`__version__ = "${versions.python}"`));
      assert(contents.has('python/CHANGELOG.md'));
      assert(![...contents.keys()].some(p => p.startsWith('python/frameworks/')), 'no sibling updates, not even unchanged version writes');
    }
    scenarios.push({files, bumped: expected, pullRequests: pulls.length});
  }
  console.log(JSON.stringify({result: 'PASS', pullRequests: prs.length, checkedJavaParents, bomPresent: javaPoms.includes('java/traceai-bom/pom.xml'), scenarios, syntheticBumps: Object.fromEntries(Object.keys(next).filter(p => next[p] !== baseline[p]).map(p => [p, {from: baseline[p], to: next[p]}])), updatedFiles: [...updates.keys()]}, null, 2));
})().catch(err => { console.error(err); process.exitCode = 1; });
