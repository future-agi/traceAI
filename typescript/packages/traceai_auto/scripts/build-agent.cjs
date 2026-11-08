// Build a relocatable launcher payload. Only Node builtins remain external.
const esbuild = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '..');
const out = path.join(root, 'dist/agent');
const instrumentationRequire = createRequire(require.resolve('@opentelemetry/instrumentation'));
const hook = instrumentationRequire.resolve('import-in-the-middle/hook.mjs');
const register = instrumentationRequire.resolve('import-in-the-middle/lib/register.js');

function packageForInput(input, cache) {
  // Metafile paths are relative to absWorkingDir, not necessarily process.cwd().
  // Resolve symlinks before walking so pnpm's installed package owns its inputs.
  let directory = path.dirname(fs.realpathSync(path.resolve(root, input)));
  const visited = [];
  while (true) {
    if (cache.has(directory)) {
      const owner = cache.get(directory);
      for (const entry of visited) cache.set(entry, owner);
      return owner;
    }
    visited.push(directory);
    let metadata;
    try {
      metadata = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    // ESM/CJS subdirectories can have a nameless {"type": ...} package.json.
    if (metadata && typeof metadata.name === 'string' && metadata.name.trim()) {
      if (typeof metadata.version !== 'string' || !metadata.version.trim()) {
        throw new Error(`Bundled package ${metadata.name} has no version (${input})`);
      }
      const owner = {
        directory, name: metadata.name, version: metadata.version,
        id: `${metadata.name}@${metadata.version}`,
        license: metadata.license === undefined ? '(not declared)' : JSON.stringify(metadata.license),
        firstParty: metadata.name.startsWith('@traceai/'),
      };
      for (const entry of visited) cache.set(entry, owner);
      return owner;
    }
    const parent = path.dirname(directory);
    if (parent === directory) throw new Error(`No named package.json owns bundled input ${input}`);
    directory = parent;
  }
}

function licenseFiles(owner) {
  const files = [];
  function visit(directory, ancestors) {
    const real = fs.realpathSync(directory);
    const relative = path.relative(owner.directory, real);
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new Error(`License scan leaves installed package at ${directory}`);
    }
    if (ancestors.has(real)) throw new Error(`Cyclic directory in license scan at ${directory}`);
    const next = new Set(ancestors); next.add(real);
    for (const name of fs.readdirSync(directory).sort()) {
      // Nested dependencies own their notices and are discovered from their inputs.
      if (name === 'node_modules' || name === '.git') continue;
      const file = path.join(directory, name), stat = fs.statSync(file);
      if (stat.isDirectory()) visit(file, next);
      else if (/^(?:LICENSE|LICENCE|COPYING|NOTICE)/i.test(name)) {
        if (!stat.isFile()) throw new Error(`License text is not a regular file: ${file}`);
        const text = fs.readFileSync(file);
        if (!text.toString('utf8').trim()) throw new Error(`Empty license text: ${file}`);
        files.push({ file: path.relative(owner.directory, file).split(path.sep).join('/'), text });
      }
    }
  }
  try {
    visit(owner.directory, new Set());
    if (!files.length) throw new Error('No LICENSE*, LICENCE*, COPYING* or NOTICE* text file');
    return files;
  } catch (error) {
    throw new Error(`Missing or unreadable bundled license text for ${owner.id}: ${error.message}`);
  }
}

function writeThirdPartyNotices(builds) {
  const cache = new Map(), packages = new Map(), manifest = {};
  for (const bundle of Object.keys(builds).sort()) {
    const inputs = builds[bundle].metafile && builds[bundle].metafile.inputs;
    if (!inputs || !Object.keys(inputs).length) throw new Error(`Missing metafile inputs for ${bundle}`);
    const ids = new Set();
    for (const input of Object.keys(inputs).sort()) {
      let owner;
      try { owner = packageForInput(input, cache); }
      catch (error) { throw new Error(`Cannot identify bundled input ${input} in ${bundle}: ${error.message}`); }
      packages.set(owner.directory, owner);
      if (!owner.firstParty) ids.add(owner.id);
    }
    // All four bundle keys are emitted, including an edge bundle with no third parties.
    // First-party packages are listed separately in the notice preamble.
    manifest[bundle] = [...ids].sort();
  }
  const firstParty = new Map(), thirdParty = new Map();
  for (const owner of packages.values()) {
    const group = owner.firstParty ? firstParty : thirdParty;
    let entry = group.get(owner.id);
    if (entry && entry.license !== owner.license) throw new Error(`Conflicting license fields for ${owner.id}`);
    if (!entry) { entry = { ...owner, files: [] }; group.set(owner.id, entry); }
    if (!owner.firstParty) {
      // Validate every installed copy even when two inputs have the same name/version.
      // Keep distinct texts at the same relative filename; omit only exact duplicates.
      for (const file of licenseFiles(owner)) {
        if (!entry.files.some(existing => existing.file === file.file && existing.text.equals(file.text))) entry.files.push(file);
      }
    }
  }
  const chunks = [];
  const append = text => chunks.push(Buffer.from(text, 'utf8'));
  append('@traceai/auto bundled third-party notices\n\n');
  append('Full installed license and notice files for inputs to all four esbuild bundles.\n');
  append('The third-party-manifest.json maps bundle paths to third-party name@version entries.\n');
  append('First-party workspace packages are excluded from that manifest and listed below.\n');
  for (const id of [...firstParty.keys()].sort()) {
    append(`\nFirst-party package: ${id}\npackage.json license: ${firstParty.get(id).license}\n`);
  }
  for (const id of [...thirdParty.keys()].sort()) {
    const owner = thirdParty.get(id);
    const files = owner.files.sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : Buffer.compare(a.text, b.text));
    append(`\n=== BEGIN THIRD-PARTY PACKAGE ${id} ===\n`);
    append(`Name: ${owner.name}\nVersion: ${owner.version}\npackage.json license: ${owner.license}\nLicense files: ${files.length}\n`);
    for (const file of files) {
      // Length framing lets the test check the entire verbatim file, even if its
      // text contains delimiters or lacks a trailing newline. Never trim its bytes.
      append(`--- BEGIN LICENSE FILE ${JSON.stringify(file.file)} (${file.text.length} bytes) ---\n`);
      chunks.push(file.text);
      append('\n--- END LICENSE FILE ---\n');
    }
    append('=== END THIRD-PARTY PACKAGE ===\n');
  }
  // No notice/manifest is written until every third-party installed copy validates.
  fs.writeFileSync(path.join(root, 'dist/THIRD-PARTY-NOTICES'), Buffer.concat(chunks));
  fs.writeFileSync(path.join(root, 'dist/third-party-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
}

async function main() {
  // This require lives in an optional runtime branch. Esbuild otherwise tolerates
  // a missing package and silently ships a launcher incapable of tracing OpenAI.
  const openai = require.resolve('@traceai/openai');
  fs.mkdirSync(out, { recursive: true });
  const common = { absWorkingDir: root, bundle: true, platform: 'node', target: 'node18', minify: true, keepNames: true, legalComments: 'linked', logLevel: 'warning', metafile: true };
  const runtime = await esbuild.build({ ...common, entryPoints: [path.join(root, 'src/register.ts')], outfile: path.join(out, 'runtime.cjs'), format: 'cjs',
    define: { TRACEAI_AGENT_BUNDLE: 'true' },
    plugins: [{ name: 'shared-hook-registry', setup(build) {
      // TS project references build fi-core's CJS prerequisite, not its optional ESM
      // distribution. Embed that fresh prerequisite rather than a stale ESM copy.
      build.onResolve({ filter: /^@traceai\/fi-core(?:\/privacy)?$/ }, args => ({ path: require.resolve(args.path) }));
      build.onResolve({ filter: /^\.\/lib\/register(?:\.js)?$/ }, args => {
        if (/import-in-the-middle[/\\]index\.js$/.test(args.importer)) return { path: './hook-register.cjs', external: true };
      });
    } }],
  });
  if (!Object.keys(runtime.metafile.inputs).some(file => fs.realpathSync(path.resolve(root, file)) === fs.realpathSync(openai))) {
    throw new Error('Launcher payload is missing the required @traceai/openai instrumentation. Build workspace dependencies first.');
  }
  // ES5-only entry: unsupported Node versions never parse the modern dependency graph.
  // Copy the WHOLE agent directory, including runtime/loader/legal-notice siblings.
  fs.writeFileSync(path.join(out, 'preload.cjs'), '"use strict";\ntry { if (Number(process.versions.node.split(".")[0]) >= 18 && !require("node:worker_threads").isInternalThread) require("./runtime.cjs"); } catch (_) { try { console.warn("[traceai:auto] Auto-instrumentation could not load; the application will continue."); } catch (_) {} }\n');
  // import-in-the-middle generates an import to this runtime bridge. Keep it beside
  // the loader, not in node_modules, so moving the directory remains supported.
  const hookRegister = await esbuild.build({ ...common, entryPoints: [register], outfile: path.join(out, 'hook-register.cjs'), format: 'cjs' });
  const hookModule = await esbuild.build({ ...common, entryPoints: [hook], outfile: path.join(out, 'hook.mjs'), format: 'esm',
    banner: { js: 'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);' },
    plugins: [{ name: 'relocatable-hook', setup(build) {
      build.onLoad({ filter: /import-in-the-middle[/\\]hook\.js$/ }, args => ({
        contents: fs.readFileSync(args.path, 'utf8').replace("new URL('lib/register.js', meta.url)", "new URL('./hook-register.cjs', meta.url)"),
        resolveDir: path.dirname(args.path), loader: 'js',
      }));
    } }],
  });
  const edge = await esbuild.build({ absWorkingDir: root, entryPoints: [path.join(root, 'src/edge.ts')], outfile: path.join(root, 'dist/edge/index.mjs'),
    bundle: true, platform: 'browser', format: 'esm', target: 'es2022', minify: true, legalComments: 'linked', logLevel: 'warning', metafile: true });
  writeThirdPartyNotices({
    'dist/agent/runtime.cjs': runtime,
    'dist/agent/hook-register.cjs': hookRegister,
    'dist/agent/hook.mjs': hookModule,
    'dist/edge/index.mjs': edge,
  });
  for (const file of fs.readdirSync(out)) if (!file.endsWith('.LEGAL.txt')) console.log(`agent/${file}: ${fs.statSync(path.join(out, file)).size} bytes`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
