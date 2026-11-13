// The application's own OpenTelemetry setup, registered the way NodeSDK.start()
// registers it: a global tracer provider, context manager, propagator and meter
// provider. Load it with -r before the agent, or require it after the agent's
// preload, to model an application that starts its SDK first or later.
const { trace, context, propagation, metrics } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { W3CTraceContextPropagator } = require('@opentelemetry/core');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const spans = [];
const exporter = { export(batch, done) { spans.push(...batch); done({ code: 0 }); }, async shutdown() {} };
const provider = new BasicTracerProvider({ resource: resourceFromAttributes({ 'service.name': 'own-app' }), spanProcessors: [new SimpleSpanProcessor(exporter)] });
const registered = {
  trace: trace.setGlobalTracerProvider(provider),
  context: context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()),
  propagation: propagation.setGlobalPropagator(new W3CTraceContextPropagator()),
  // A complete no-op meter provider: instrumentations create their histograms on it.
  metrics: metrics.setGlobalMeterProvider(metrics.getMeterProvider()),
};
if (process.env.APP_HTTP === 'true') {
  // The application's own incoming and outgoing HTTP spans, on its own provider.
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  const { HttpInstrumentation } = require('@opentelemetry/instrumentation-http');
  registerInstrumentations({ tracerProvider: provider, instrumentations: [new HttpInstrumentation()] });
}
globalThis.__app = { provider, spans, registered };
