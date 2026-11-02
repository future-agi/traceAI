const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { RedisInstrumentation } = require('../dist/src');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
const { NodeTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = coreRequire('@opentelemetry/sdk-trace-node');
const redisRequire = process.env.REDIS_TEST_ROOT ? createRequire(`${process.env.REDIS_TEST_ROOT}/package.json`) : require;
(async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new RedisInstrumentation();
  instrumentation.setTracerProvider(provider);
  const redis = redisRequire('redis');
  const client = redis.createClient();
  // Auto instrumentation plus optional manual setup must not double-wrap.
  instrumentation.manuallyInstrument(redis);
  instrumentation.instrumentClient(client);
  await assert.rejects(client.hGet('key', 'field'));
  assert.equal(exporter.getFinishedSpans().length, 1);
  instrumentation.disable();
  await assert.rejects(client.hGet('key', 'field'));
  assert.equal(exporter.getFinishedSpans().length, 1);
  instrumentation.enable();
  await assert.rejects(client.hGet('key', 'field'));
  assert.equal(exporter.getFinishedSpans().length, 2);
  instrumentation.disable();
  await provider.shutdown();
})().catch((error) => { console.error(error); process.exitCode = 1; });
