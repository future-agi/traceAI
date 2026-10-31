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
async function main() {
  // This require lives in an optional runtime branch. Esbuild otherwise tolerates
  // a missing package and silently ships a launcher incapable of tracing OpenAI.
  const openai = require.resolve('@traceai/openai');
  fs.mkdirSync(out, { recursive: true });
  const common = { bundle: true, platform: 'node', target: 'node18', minify: true, keepNames: true, legalComments: 'linked', logLevel: 'warning' };
  const runtime = await esbuild.build({ ...common, entryPoints: [path.join(root, 'src/register.ts')], outfile: path.join(out, 'runtime.cjs'), format: 'cjs', metafile: true,
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
  if (!Object.keys(runtime.metafile.inputs).some(file => fs.realpathSync(file) === fs.realpathSync(openai))) {
    throw new Error('Launcher payload is missing the required @traceai/openai instrumentation. Build workspace dependencies first.');
  }
  // ES5-only entry: unsupported Node versions never parse the modern dependency graph.
  // Copy the WHOLE agent directory, including runtime/loader/legal-notice siblings.
  fs.writeFileSync(path.join(out, 'preload.cjs'), '"use strict";\ntry { if (Number(process.versions.node.split(".")[0]) >= 18 && !require("node:worker_threads").isInternalThread) require("./runtime.cjs"); } catch (_) { try { console.warn("[traceai:auto] Auto-instrumentation could not load; the application will continue."); } catch (_) {} }\n');
  // import-in-the-middle generates an import to this runtime bridge. Keep it beside
  // the loader, not in node_modules, so moving the directory remains supported.
  await esbuild.build({ ...common, entryPoints: [register], outfile: path.join(out, 'hook-register.cjs'), format: 'cjs' });
  await esbuild.build({ ...common, entryPoints: [hook], outfile: path.join(out, 'hook.mjs'), format: 'esm',
    banner: { js: 'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);' },
    plugins: [{ name: 'relocatable-hook', setup(build) {
      build.onLoad({ filter: /import-in-the-middle[/\\]hook\.js$/ }, args => ({
        contents: fs.readFileSync(args.path, 'utf8').replace("new URL('lib/register.js', meta.url)", "new URL('./hook-register.cjs', meta.url)"),
        resolveDir: path.dirname(args.path), loader: 'js',
      }));
    } }],
  });
  await esbuild.build({ entryPoints: [path.join(root, 'src/edge.ts')], outfile: path.join(root, 'dist/edge/index.mjs'),
    bundle: true, platform: 'browser', format: 'esm', target: 'es2022', minify: true, legalComments: 'linked', logLevel: 'warning' });
  for (const file of fs.readdirSync(out)) if (!file.endsWith('.LEGAL.txt')) console.log(`agent/${file}: ${fs.statSync(path.join(out, file)).size} bytes`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
