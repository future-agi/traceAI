// Loaded with -r, before the agent's --import entry. As in round4.cjs, it lets the agent load
// @traceai/openai-agents from its TypeScript source, so that package needs no build of its own.
// It also turns on the OpenAI Agents SDK's own tracing, which the SDK turns off when NODE_ENV
// is test, and removes the exporter the SDK installs for OpenAI's servers, so the only trace
// processor left is the one the agent adds. It registers no OpenTelemetry.
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module'), esbuild = require('esbuild');
const source = path.resolve(__dirname, '../../../traceai_openai_agents/src/index.ts');
require.extensions['.ts'] = (module, file) => module._compile(esbuild.transformSync(fs.readFileSync(file, 'utf8'), { loader: 'ts', format: 'cjs', target: 'node20' }).code, file);
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, ...args) { return request === '@traceai/openai-agents' ? source : Reflect.apply(resolve, this, [request, ...args]); };
// The agent patches this module when the application requires it again, from the cache.
const agents = require('@openai/agents');
agents.setTracingDisabled(false);
agents.setTraceProcessors([]);
