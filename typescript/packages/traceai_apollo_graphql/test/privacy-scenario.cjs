// A fresh process per policy: real Apollo execution and the actual exporter boundary.
const assert = require('node:assert/strict');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { InMemorySpanExporter } = require('@opentelemetry/sdk-trace');
const { trace } = require('@opentelemetry/api');
const memory = new InMemorySpanExporter();
OTLPTraceExporter.prototype.export = function(spans, callback) { memory.export(spans, callback); };
OTLPTraceExporter.prototype.shutdown = async function() {};
const telemetry = require('@traceai/apollo-graphql').configure({
  apiKey: 'synthetic', secretKey: 'synthetic', baseUrl: 'http://127.0.0.1:9',
  ...JSON.parse(process.env.PRIVACY_OPTIONS || '{}'),
});
(async () => {
  const { ApolloServer } = await import('@apollo/server');
  const { startStandaloneServer } = await import('@apollo/server/standalone');
  const server = new ApolloServer({
    stopOnTerminationSignals: false,
    typeDefs: 'enum Choice { PRIVATE_ENUM } type Query { hello(choice: Choice): String, fail: String }',
    resolvers: { Query: { hello: () => 'ok', fail: () => { throw new Error('PRIVATE_ERROR review.person@example.test'); } } },
  });
  const { url } = await startStandaloneServer(server, { listen: { host: '127.0.0.1', port: 0 } });
  try {
    const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query: 'query Review { hello(choice: PRIVATE_ENUM) fail }' }) });
    const result = await response.json();
    assert.equal(result.data.hello, 'ok');
    assert.equal(result.errors.length, 1);
    // Upstream currently sets only a status code. Pin the boundary's treatment of
    // descriptions and retained metadata too, in case instrumentation adds them.
    const probe = trace.getTracer('@opentelemetry/instrumentation-graphql').startSpan('privacy-boundary');
    probe.setStatus({ code: 2, message: 'PRIVATE_STATUS review.person@example.test' });
    probe.setAttribute('graphql.field.type', ['review.person@example.test', 'String']);
    probe.end();
  } finally {
    await server.stop();
    assert.equal(await telemetry.forceFlush(), true);
    assert.equal(await telemetry.shutdown(), true);
  }
  console.log(JSON.stringify(memory.getFinishedSpans().map(s => ({
    name: s.name, attributes: s.attributes, status: s.status, events: s.events,
  }))));
})().catch(error => { console.error(error); process.exitCode = 1; });
