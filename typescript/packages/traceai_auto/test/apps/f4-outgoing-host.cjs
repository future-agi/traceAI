'use strict';
// Synthetic application-owned SDK, installed before auto. No FI route or credentials.
// Confine this CHILD's DNS to loopback; synthetic vendor hostnames never egress.
const dns = require('node:dns');
const lookup = (_hostname, options, callback) => {
  if (typeof options === 'function') { callback = options; options = {}; }
  if (options && options.all) callback(null, [{ address: '127.0.0.1', family: 4 }]);
  else callback(null, '127.0.0.1', 4);
};
dns.lookup = lookup;
const { trace, context, propagation } = require('@opentelemetry/api');
const { AsyncLocalStorageContextManager } = require('@opentelemetry/context-async-hooks');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const http = require('node:http');
const spans = [];
const exporter = { export(batch, done) { spans.push(...batch); done({ code: 0 }); }, async shutdown() {} };
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const manager = new AsyncLocalStorageContextManager().enable();
const custom = { inject(_ctx, carrier, setter) { setter.set(carrier, 'x-private-custom', 'must-not-be-injected'); }, extract(ctx) { return ctx; }, fields() { return ['x-private-custom']; } };
trace.setGlobalTracerProvider(provider);
context.setGlobalContextManager(manager);
propagation.setGlobalPropagator(custom);
globalThis.__f4 = { provider, manager, custom, spans, lookup, cachedRequest: http.request, cachedGet: http.get };
