// Fresh-process sampler probe: no network, credentials or application SDK hooks.
const { createTraceState, diag, DiagLogLevel, ROOT_CONTEXT, TraceFlags, trace } = require('@opentelemetry/api');
const { AlwaysOffSampler, AlwaysOnSampler, BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');

(async () => {
  const warnings = [], diagnostics = [];
  const warn = console.warn, error = console.error;
  const diagnostic = (source, level, args) => diagnostics.push({ source, level, message: args.join(' ') });
  console.warn = (...args) => {
    warnings.push(args.join(' '));
    diagnostic('console', 'warn', args);
    if (process.env.TEST_THROW_WARNING === 'true') throw new Error('Synthetic logger failure');
  };
  console.error = (...args) => diagnostic('console', 'error', args);
  if (process.env.TEST_OTEL_DIAGNOSTICS === 'true') {
    diag.setLogger({
      warn: (...args) => diagnostic('otel', 'warn', args),
      error: (...args) => diagnostic('otel', 'error', args),
      info() {}, debug() {}, verbose() {},
    }, DiagLogLevel.ALL);
  }
  const agent = require('../../dist/src/index.js');
  const exporter = new InMemorySpanExporter();
  const options = { projectName: 'sampler-fixture', instrumentations: ['wire'], wireCapture: false };
  const failRegistration = process.env.TEST_REGISTER_FAILURE === 'true';
  if (failRegistration) options.projectName = ''; // register rejects this inside the construction scope.
  if (process.env.TEST_ENDPOINT) options.baseUrl = process.env.TEST_ENDPOINT;
  if (process.env.TEST_SAMPLE_RATE !== undefined) options.sampleRate = Number(process.env.TEST_SAMPLE_RATE);
  let host, hostCalls = 0;
  const hostMode = process.env.TEST_HOST;
  if (hostMode) {
    const delegate = process.env.TEST_HOST_SAMPLER === 'off' ? new AlwaysOffSampler() : new AlwaysOnSampler();
    host = new BasicTracerProvider({
      sampler: { shouldSample(...args) { hostCalls++; return delegate.shouldSample(...args); }, toString: () => 'ApplicationSampler' },
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    if (hostMode === 'global') trace.setGlobalTracerProvider(host);
    else options.provider = host;
    if (hostMode !== 'only') {
      options.forwardApplicationSpans = true;
      // Separate agent destination; the application's exporter remains independent.
      options.exporter = new InMemorySpanExporter();
    }
  } else options.exporter = exporter;
  const samplerKeys = ['OTEL_TRACES_SAMPLER', 'OTEL_TRACES_SAMPLER_ARG'];
  const samplerEnv = samplerKeys.map(key => process.env[key]);
  let result;
  try {
    if (process.env.TEST_OTEL_DIAGNOSTICS === 'true') {
      diag.warn('sampler fixture: before init warning');
      diag.error('sampler fixture: before init error');
    }
    const provider = agent.init(options);
    if (failRegistration ? provider !== undefined : !provider) throw new Error('Unexpected sampler fixture initialization result');
    if (samplerKeys.some((key, index) => process.env[key] !== samplerEnv[index])) {
      throw new Error('Agent changed the application sampler environment during init');
    }
    const tracer = provider?.getTracer('sampler-fixture');
    const remote = process.env.TEST_REMOTE !== 'false';
    // Fix generated roots as well as parent trace IDs, without probabilistic assertions.
    if (tracer && process.env.TEST_TRACE_ID) tracer._idGenerator.generateTraceId = () => process.env.TEST_TRACE_ID;
    const parent = sampled => trace.setSpanContext(ROOT_CONTEXT, {
      traceId: process.env.TEST_TRACE_ID || '1'.repeat(32), spanId: '2'.repeat(16),
      traceFlags: sampled ? TraceFlags.SAMPLED : TraceFlags.NONE, isRemote: remote,
      traceState: process.env.TEST_TRACESTATE ? createTraceState(process.env.TEST_TRACESTATE) : undefined,
    });
    const { FITracer } = require('@traceai/fi-core');
    const startTracer = provider && process.env.TEST_AUTO_TRACER === 'true'
      ? new FITracer({ tracer: FITracer.autoProvider(provider).getTracer('sampler-fixture') }) : tracer;
    const attributes = Object.freeze(Object.fromEntries(Array.from({ length: Number(process.env.TEST_ATTRIBUTES_COUNT || 0) }, (_, i) => [`existing.${i}`, 'value'])));
    let rootContext;
    const inputs = [['root', ROOT_CONTEXT], ['sampled-parent', parent(true)], ['unsampled-parent', parent(false)]];
    if (process.env.TEST_LOCAL_CHILD === 'true') inputs.push(['local-child', undefined]);
    const spans = tracer ? inputs.map(([name, context]) => {
      const span = startTracer.startSpan(name, { attributes }, name === 'local-child' ? rootContext : context);
      if (name === 'root') rootContext = trace.setSpanContext(ROOT_CONTEXT, span.spanContext());
      const result = { name, recording: span.isRecording(), sampled: !!(span.spanContext().traceFlags & TraceFlags.SAMPLED) };
      span.end();
      return result;
    }) : [];
    await agent.forceFlush();
    await agent.forceFlush();
    const exported = exporter.getFinishedSpans().map(span => ({
      name: span.name, sampled: !!(span.spanContext().traceFlags & TraceFlags.SAMPLED),
      attributes: span.attributes, traceState: span.spanContext().traceState?.serialize(),
    }));
    result = { spans, exported, warnings, diagnostics, sameProvider: provider === host, hostCalls };
  } finally {
    await agent.shutdown();
    await host?.shutdown();
    if (process.env.TEST_OTEL_DIAGNOSTICS === 'true') {
      diag.warn('sampler fixture: after shutdown warning');
      diag.error('sampler fixture: after shutdown error');
      diag.disable();
    }
    console.warn = warn;
    console.error = error;
  }
  if (samplerKeys.some((key, index) => process.env[key] !== samplerEnv[index])) {
    throw new Error('Agent changed the application sampler environment');
  }
  process.stdout.write(JSON.stringify(result));
})().catch(error => { console.error(error); process.exitCode = 1; });
