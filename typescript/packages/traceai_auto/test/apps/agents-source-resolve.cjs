// Loaded with -r, before the agent when the agent is to be loaded before the OpenAI Agents SDK. As in agents-source-preload.cjs, it lets the agent
// load @traceai/openai-agents from its TypeScript source, so that package needs no build of its own. It loads neither the SDK nor undici.
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module'), esbuild = require('esbuild');
const source = path.resolve(__dirname, '../../../traceai_openai_agents/src/index.ts');
require.extensions['.ts'] = (module, file) => module._compile(esbuild.transformSync(fs.readFileSync(file, 'utf8'), { loader: 'ts', format: 'cjs', target: 'node20' }).code, file);
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, ...args) { return request === '@traceai/openai-agents' ? source : Reflect.apply(resolve, this, [request, ...args]); };
