// Source-only preload: register no context manager, tracer provider, or propagator.
// The application gets the launcher module only so it can flush before reporting.
const { resolve } = require('node:path');
require('ts-node').register({
  project: resolve(__dirname, '../../tsconfig.json'),
  transpileOnly: true,
});
const { context } = require('@opentelemetry/api');
globalThis.__portkeyNoOtelTest = {
  contextManager: () => context._getContextManager().constructor.name,
  auto: require('../../../traceai_auto/src/index.ts'),
};
