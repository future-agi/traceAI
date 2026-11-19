const mode = process.env.MODE;
(async () => {
  const {trace, context} = require('@opentelemetry/api');
  let keepalive, shutdownCalls = 0;
  const exporter = {export(_spans, callback) {callback({code:0});},
    forceFlush() { return mode === 'stalled-flush' ? new Promise(() => {}) : mode === 'rejected-flush' ? Promise.reject(new Error('flush failed')) : Promise.resolve(); },
    shutdown() {shutdownCalls++; if (keepalive) clearInterval(keepalive); return Promise.resolve();}};
  if (mode === 'sigterm-once') {
    keepalive = setInterval(() => {}, 1000);
    process.once('SIGTERM', () => {
      console.log('app-handler-start');
      setTimeout(() => {clearInterval(keepalive); console.log('app-handler-finished');}, 100);
    });
  }
  const auto = require('@traceai/auto');
  if (mode.startsWith('rollback')) {
    let provider, manager;
    if (mode === 'rollback-existing') {
      const {BasicTracerProvider, SimpleSpanProcessor} = require('@opentelemetry/sdk-trace-base');
      const {AsyncLocalStorageContextManager} = require('@opentelemetry/context-async-hooks');
      provider = new BasicTracerProvider({spanProcessors:[new SimpleSpanProcessor({export(){},shutdown:async()=>{}})]});
      trace.setGlobalTracerProvider(provider); manager = new AsyncLocalStorageContextManager().enable(); context.setGlobalContextManager(manager);
    }
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'fetch');
    Object.defineProperty(globalThis, 'fetch', {...descriptor,writable:false});
    auto.init({exporter}); const first = auto.report();
    const afterFailure = trace.getTracerProvider().getDelegate();
    const processorsAfterFailure = provider?._activeSpanProcessor._spanProcessors.length;
    const contextPreserved = manager ? context._getContextManager() === manager : context._getContextManager().constructor.name === 'NoopContextManager';
    Object.defineProperty(globalThis, 'fetch', descriptor);
    auto.init({exporter}); const second = auto.report();
    await auto.shutdown();
    console.log(JSON.stringify({first:first.active,second:second.active,rolledBack:provider ? afterFailure === provider : afterFailure.constructor.name === 'NoopTracerProvider',contextPreserved,processorsAfterFailure,processorsAfterShutdown:provider?._activeSpanProcessor._spanProcessors.length,shutdownCalls}));
    return;
  }
  auto.init({exporter});
  if (mode === 'sigterm-once') {process.kill(process.pid, 'SIGTERM'); return;}
  keepalive = setInterval(() => {}, 1000);
  if (mode === 'stalled-decoder') globalThis[Symbol.for('traceai.auto.agent.v1')].fetch.pending.add(new Promise(() => {}));
  const start = Date.now(); await auto.shutdown();
  console.log(JSON.stringify({elapsed:Date.now()-start,shutdownCalls,keepaliveDestroyed:keepalive._destroyed}));
})().catch(error => {console.error(error); process.exitCode = 1;});
