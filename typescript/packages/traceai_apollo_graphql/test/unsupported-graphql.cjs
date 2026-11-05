// Run only in the deliberately unsupported GraphQL 17 consumer, not the matrix.
const assert = require('node:assert/strict');
const { trace, ProxyTracerProvider } = require('@opentelemetry/api');
const { configure } = require('@traceai/apollo-graphql');
const { dirname, join } = require('node:path');
assert.equal(require(join(dirname(require.resolve('graphql')), 'package.json')).version, '17.0.2');
assert.throws(() => configure({
  apiKey: 'synthetic-key', secretKey: 'synthetic-secret', baseUrl: 'http://127.0.0.1:9',
}), /requires GraphQL >=16\.11\.0 <17/);
assert.ok(trace.getTracerProvider() instanceof ProxyTracerProvider);
assert.equal(require.cache[require.resolve('graphql')], undefined);
console.log('GraphQL 17 is explicitly rejected before instrumentation or SDK startup');
