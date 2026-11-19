// Run once per SDK version pair in an isolated process (RTC loads native libraries).
// LIVEKIT_COMPAT_DIR points at an npm prefix containing the real SDKs.
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const { resolve } = require('node:path');
const http = require('node:http');
const { test } = require('node:test');
const packageRequire = createRequire(resolve(process.env.LIVEKIT_INSTRUMENTATION_DIR || resolve(__dirname, '..'), 'package.json'));
const { LiveKitInstrumentation, isPatched } = packageRequire('@traceai/fi-instrumentation-livekit');
const { context, trace } = packageRequire('@opentelemetry/api');
const coreRequire = createRequire(packageRequire.resolve('@traceai/fi-core'));
const { register, ProjectType } = packageRequire('@traceai/fi-core');
const { AsyncLocalStorageContextManager } = coreRequire('@opentelemetry/context-async-hooks');
const sdkRequire = createRequire(resolve(process.env.LIVEKIT_COMPAT_DIR, 'package.json'));

function attributes(span) { return Object.fromEntries(span.attributes.map(({ key, value }) => [key, Object.values(value)[0]])); }

test('real SDK methods export one span each through loopback OTLP', { timeout: 30000 }, async t => {
  const batches = [];
  const receiver = http.createServer((req, res) => {
    assert.equal(req.url, '/tracer/v1/traces');
    assert.equal(req.headers['x-api-key'], 'synthetic-api-key');
    assert.equal(req.headers['x-secret-key'], 'synthetic-secret-key');
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => { batches.push(JSON.parse(body)); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}'); });
  });
  await new Promise(resolve => receiver.listen(0, '127.0.0.1', resolve));
  const provider = register({
    projectName: 'livekit-contract',
    projectType: ProjectType.OBSERVE,
    endpoint: `http://127.0.0.1:${receiver.address().port}/tracer/v1/traces`,
    headers: { 'x-api-key': 'synthetic-api-key', 'x-secret-key': 'synthetic-secret-key' },
    setGlobalTracerProvider: false,
  });
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  const instrumentation = new LiveKitInstrumentation();
  instrumentation.setTracerProvider(provider);
  const semver = createRequire(packageRequire.resolve('@opentelemetry/instrumentation'))('semver');
  for (const definition of instrumentation.init()) {
    const version = require(resolve(process.env.LIVEKIT_COMPAT_DIR, 'node_modules', definition.name, 'package.json')).version;
    assert.ok(definition.supportedVersions.some(range => semver.satisfies(version, range)), `${definition.name}@${version} must match its loader filter`);
  }
  const sources = [];
  let rtc;
  t.after(async () => {
    instrumentation.disable();
    for (const source of sources) if (source.close) await source.close();
    await provider.shutdown();
    context.disable();
    manager.disable();
    await new Promise(resolve => receiver.close(resolve));
    await rtc?.dispose?.();
  });

  // CommonJS SDK loading exercises the actual automatic loader hook.
  const client = sdkRequire('livekit-client');
  assert.equal(isPatched(), true, 'client auto-loader must patch this SDK version');
  const room = new client.Room();
  const parent = provider.getTracer('contract-test').startSpan('parent');
  await context.with(trace.setSpan(context.active(), parent), () => room.disconnect());
  // An extra manual registration must not duplicate automatic instrumentation.
  instrumentation.manuallyInstrument(client);
  await room.disconnect();
  parent.end();

  // rtc-node is ESM. Instrument the real (non-extensible) namespace, not a copied mock.
  rtc = await import(pathToFileURL(sdkRequire.resolve('@livekit/rtc-node')).href);
  assert.equal(Object.isExtensible(rtc), false);
  instrumentation.manuallyInstrument(rtc);
  const rtcRoom = new rtc.Room();
  await assert.rejects(rtcRoom.connect('not-a-url', 'synthetic-token'));
  await rtcRoom.disconnect();
  const audio = new rtc.AudioSource(48000, 1);
  sources.push(audio);
  await audio.captureFrame(new rtc.AudioFrame(new Int16Array(480), 48000, 1, 480));
  const video = new rtc.VideoSource(16, 16);
  sources.push(video);
  // I420 is supported across the range; rtc-node 0.3.0 has an upstream RGBA stride bug.
  const frame = new rtc.VideoFrame(new Uint8Array(16 * 16 * 3 / 2), 16, 16, rtc.VideoBufferType.I420);
  assert.equal(video.captureFrame(frame), undefined, 'video capture remains synchronous');
  await provider.forceFlush();
  assert.ok(batches.length > 0, 'the Future AGI exporter must send OTLP');
  for (const resource of batches.flatMap(batch => batch.resourceSpans)) {
    assert.equal(attributes(resource.resource).project_name, 'livekit-contract');
    assert.equal(attributes(resource.resource).project_type, ProjectType.OBSERVE);
  }
  const spans = batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
  const operationSpans = spans.filter(span => attributes(span)['livekit.operation']);
  assert.deepEqual(operationSpans.map(span => attributes(span)['livekit.operation']).sort(),
    ['audio.capture', 'room.connect', 'room.disconnect', 'room.disconnect', 'room.disconnect', 'video.capture']);
  const disconnect = operationSpans.find(span => span.parentSpanId);
  assert.equal(disconnect.parentSpanId, parent.spanContext().spanId);
  assert.equal(disconnect.traceId, parent.spanContext().traceId);
  for (const span of operationSpans) {
    assert.equal(attributes(span)['fi.span.kind'], 'CHAIN');
    assert.equal(span.status.code, attributes(span)['livekit.operation'] === 'room.connect' ? 2 : 1);
    assert.ok(BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano));
  }
  assert.equal(Number(attributes(operationSpans.find(span => attributes(span)['livekit.operation'] === 'audio.capture'))['livekit.sample_rate']), 48000);
  instrumentation.disable();
  assert.equal(isPatched(), false);
  await room.disconnect();
  await provider.forceFlush();
  assert.equal(batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans))).length, spans.length);
  instrumentation.enable();
  await room.disconnect();
  await provider.forceFlush();
  assert.equal(batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans))).length, spans.length + 1,
    'the same client Room must resume tracing after re-enable');
  instrumentation.manuallyInstrument(client);
  await room.disconnect();
  await new client.Room().disconnect();
  await provider.forceFlush();
  assert.equal(batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans))).length, spans.length + 3,
    'an already-loaded CommonJS constructor must work after re-enable');

  const shimmer = createRequire(packageRequire.resolve('@opentelemetry/instrumentation'))('shimmer');
  const exportedSpans = () => batches.flatMap(batch => batch.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
  for (const registration of ['enable', 'instrumentRoom']) {
    for (const wrapperKind of ['plain', 'shimmer']) {
      const retained = new client.Room();
      let thirdPartyCalls = 0;
      const wrap = original => function (...args) {
        thirdPartyCalls++;
        return original.apply(this, args);
      };
      if (wrapperKind === 'shimmer') shimmer.wrap(retained, 'disconnect', wrap);
      else retained.disconnect = wrap(retained.disconnect);
      const outer = retained.disconnect;
      for (let cycle = 0; cycle < 3; cycle++) {
        if (registration === 'enable') instrumentation.enable();
        else instrumentation.instrumentRoom(retained);
        const before = exportedSpans().length;
        await retained.disconnect();
        await provider.forceFlush();
        assert.equal(exportedSpans().length - before, 1,
          `${registration} under ${wrapperKind} wrapper must export exactly one disconnect span`);
        assert.equal(retained.disconnect, outer, 'registration must preserve the third-party wrapper');
        assert.equal(thirdPartyCalls, cycle + 1);
      }
    }
  }
});
