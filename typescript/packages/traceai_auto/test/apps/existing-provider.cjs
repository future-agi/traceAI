const { trace, context } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const collected = [];
const exporter = { export(spans, done) { collected.push(...spans); done({ code: 0 }); }, async shutdown() {} };
const provider = new BasicTracerProvider({ resource: resourceFromAttributes({ 'service.name': 'existing-app' }), spanProcessors: [new SimpleSpanProcessor(exporter)] });
trace.setGlobalTracerProvider(provider);
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
process.on('SIGINT', () => {}); process.on('SIGTERM', () => {});
process.on('exit', () => {
  const global = trace.getTracerProvider();
  console.log(JSON.stringify({ existing: (global.getDelegate?.() || global) === provider, collected: collected.length, sigint: process.listenerCount('SIGINT'), sigterm: process.listenerCount('SIGTERM') }));
});
