const { createRequire } = require('node:module');
const { resolve } = require('node:path');
const loadCore = createRequire(resolve(__dirname, '../package.json'));
const loadAuto = createRequire(resolve(__dirname, '../../traceai_auto/package.json'));
const core = loadCore('@traceai/fi-core');
const auto = loadAuto('@traceai/auto');
const sdk = loadCore('@opentelemetry/sdk-trace-base');
const { trace, context, propagation } = loadCore('@opentelemetry/api');
const id = process.argv[2];
function check(value, caseId) { if (!value) throw new Error(caseId); }
function leaves(processor) {
  if (Array.isArray(processor._spanProcessors)) return processor._spanProcessors.flatMap(leaves);
  if (Array.isArray(processor.processors)) return processor.processors.flatMap(leaves);
  return [processor];
}
async function run() {
  const manual = [];
  const delivered = [];
  let flushes = 0, shutdowns = 0;
  const exporter = {
    export(spans, done) { delivered.push(...spans); done({ code: 0 }); },
    async forceFlush() { flushes++; }, async shutdown() { shutdowns++; },
  };
  try {
    if (id === 'R-C1-BATCH-POLICY') {
      const options = { setGlobalTracerProvider: false, headers: {}, endpoint: 'http://127.0.0.1:1', projectName: 'c1' };
      manual.push(core.register({ ...options, batch: false }), core.register(options));
      for (const provider of manual) {
        const processors = leaves(provider._activeSpanProcessor);
        check(processors.filter(p => p instanceof sdk.SimpleSpanProcessor).length === 1, 'R-C1-BATCH-POLICY');
        check(!processors.some(p => p instanceof sdk.BatchSpanProcessor), 'R-C1-BATCH-POLICY');
      }
    }
    const provider = auto.init({ exporter, instrumentations: ['wire'], wireCapture: false, configFile: '/nonexistent/traceai-c1.toml' });
    check(!!provider && auto.report().active, 'R-C1-BATCH-POLICY');
    const batches = leaves(provider._activeSpanProcessor).filter(p => p instanceof core.BatchSpanProcessor);
    check(batches.length === 1 && Number.isFinite(batches[0]._maxQueueSize) && batches[0]._maxQueueSize > 0 &&
      Number.isFinite(batches[0]._maxExportBatchSize) && batches[0]._maxExportBatchSize > 0, 'R-C1-BATCH-POLICY');
    if (id === 'R-C1-LAMBDA-BATCH') {
      let completed = false;
      const handler = core.traceLambdaHandler(async () => {
        provider.getTracer('c1').startSpan('child').end();
        check(delivered.length === 0, 'R-C1-LAMBDA-BATCH');
        await Promise.resolve();
        completed = true;
        return 7;
      }, { provider });
      const result = await handler({}, { functionName: 'c1', awsRequestId: 'c1', getRemainingTimeInMillis: () => 10000 });
      check(result === 7 && completed, 'R-C1-LAMBDA-BATCH');
      check(delivered.length === 2 && delivered.some(s => s.name === 'child') && delivered.some(s => s.name === 'aws.lambda.invoke'), 'R-C1-LAMBDA-BATCH');
      check(flushes > 0 && shutdowns === 0 && auto.report().active, 'R-C1-LAMBDA-BATCH');
    } else {
      provider.getTracer('c1').startSpan('queued').end();
      check(delivered.length === 0, 'R-C1-BATCH-POLICY');
      await provider.forceFlush();
      check(delivered.length === 1 && delivered[0].name === 'queued' && flushes > 0, 'R-C1-BATCH-POLICY');
    }
  } finally {
    await auto.shutdown();
    for (const provider of manual) await provider.shutdown();
    trace.disable(); context.disable(); propagation.disable();
  }
}
run().catch(() => { process.exitCode = 1; });
