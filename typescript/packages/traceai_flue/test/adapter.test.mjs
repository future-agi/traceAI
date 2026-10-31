import assert from 'node:assert/strict';
import test from 'node:test';
import { diag, metrics, trace } from '@opentelemetry/api';
import { createOpenTelemetryInstrumentation } from '@flue/opentelemetry';
import { instrument } from '@flue/runtime';
import { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } from '@opentelemetry/sdk-trace-base';
import { configure } from '../dist/index.js';
import { events, recorder, waitFor } from './helpers.mjs';

const settings = (sink, extra = {}) => ({ apiKey: 'synthetic-api', secretKey: 'synthetic-secret', projectName: 'synthetic-project', baseUrl: sink.url, captureContent: false, ...extra });

test('baseline official adapter has no FI destination mapping', async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const dispose = instrument(createOpenTelemetryInstrumentation({ tracer: provider.getTracer('baseline'), content: false }));
  try {
    const fixture = events(); fixture.start(); fixture.end();
    await provider.forceFlush();
    const [span] = exporter.getFinishedSpans();
    assert.equal(span.attributes['gen_ai.operation.name'], 'invoke_agent');
    assert.equal(span.attributes['fi.span.kind'], undefined);
    assert.equal(span.attributes['session.id'], undefined);
  } finally { await dispose(); await provider.shutdown(); }
});

test('native spans only, private resource, session/kind mapping and no global OTel side effects', async () => {
  const sink = await recorder();
  const before = trace.getTracerProvider();
  let meterAccess = 0;
  metrics.setGlobalMeterProvider({ getMeter() { meterAccess++; throw new Error('must not access global meter'); } });
  const oldCompression = process.env.OTEL_EXPORTER_OTLP_COMPRESSION;
  process.env.OTEL_EXPORTER_OTLP_COMPRESSION = 'gzip';
  const handle = configure(settings(sink));
  try {
    assert.equal(configure(settings(sink)), handle);
    const fixture = events(); fixture.start(); fixture.tool(); fixture.end();
    await handle.flush();
    assert.equal(sink.requests.length, 1);
    const [request] = sink.requests;
    assert.equal(request.url, '/tracer/v1/traces');
    assert.equal(request.headers['x-api-key'], 'synthetic-api');
    assert.equal(request.headers['x-secret-key'], 'synthetic-secret');
    assert.equal(request.headers['content-type'], 'application/x-protobuf');
    assert.equal(request.headers['content-encoding'], undefined);
    assert.equal(request.spans.length, 2, 'one root and one tool; no duplicate engine');
    const root = request.spans.find((span) => span.name === 'invoke_agent Weather');
    const tool = request.spans.find((span) => span.name === 'execute_tool weather');
    assert.equal(root.attributes['fi.span.kind'], 'AGENT');
    assert.equal(tool.attributes['fi.span.kind'], 'TOOL');
    assert.equal(tool.parentSpanId, root.spanId);
    assert.equal(tool.traceId, root.traceId);
    assert.equal(tool.status.code, 2);
    for (const span of request.spans) {
      assert.equal(span.attributes['session.id'], 'conversation');
      assert.deepEqual(span.resource, { project_name: 'synthetic-project', project_type: 'observe', 'service.name': 'synthetic-project' });
    }
    assert.doesNotMatch(request.body, /PRIVATE_|synthetic-api|synthetic-secret|process\.owner|host\.name/);
    assert.equal(trace.getTracerProvider(), before);
    assert.equal(meterAccess, 0);
    const stop = handle.shutdown();
    assert.equal(handle.shutdown(), stop);
    await stop;
    fixture.start(); fixture.end();
    assert.equal(sink.requests.length, 1, 'shutdown unregisters the producer');
  } finally {
    await handle.shutdown(); await sink.close(); metrics.disable();
    if (oldCompression === undefined) delete process.env.OTEL_EXPORTER_OTLP_COMPRESSION;
    else process.env.OTEL_EXPORTER_OTLP_COMPRESSION = oldCompression;
  }
});

test('content opt-in redacts via native policy and always removes stacks', async () => {
  const sink = await recorder();
  const handle = configure(settings(sink, { captureContent: true, transform: (content) => typeof content === 'string' ? content.replaceAll('PRIVATE', 'REDACTED') : content }));
  try {
    const fixture = events(); fixture.start(); fixture.tool(); fixture.end();
    await handle.shutdown();
    const body = sink.requests.map((r) => r.body).join('');
    assert.match(body, /REDACTED_ERROR/);
    assert.doesNotMatch(body, /PRIVATE_STACK|exception\.stacktrace|PRIVATE_ERROR/);
    assert.match(body, /gen_ai\.input\.messages/);
  } finally { await handle.shutdown(); await sink.close(); }
});

test('submission flushes deliver each batch even when an earlier request is in flight', async () => {
  const sink = await recorder({ delay: 100 });
  const handle = configure(settings(sink));
  try {
    const first = events('1'); first.start(); first.end(); first.emit({ type: 'submission_settled' });
    await waitFor(() => sink.requests.length === 1);
    const second = events('2'); second.start(); second.end(); second.emit({ type: 'submission_settled' });
    await handle.shutdown();
    assert.equal(sink.requests.flatMap((r) => r.spans).length, 2);
  } finally { await handle.shutdown(); await sink.close(); }
});

test('shutdown ends unfinished native spans once and permits later reconfiguration', async () => {
  const sink = await recorder();
  const count = process.listenerCount('beforeExit');
  let handle = configure(settings(sink));
  try {
    events().start();
    await handle.shutdown();
    assert.equal(process.listenerCount('beforeExit'), count);
    assert.equal(sink.requests[0].spans[0].attributes['error.type'], 'interrupted');
    handle = configure(settings(sink));
    const fixture = events('new'); fixture.start(); fixture.end();
    await handle.shutdown();
    assert.equal(sink.requests.flatMap((r) => r.spans).length, 2);
  } finally { await handle.shutdown(); await sink.close(); }
});

test('invalid settings and another native registration never leak an exit handler', async () => {
  const sink = await recorder();
  try {
    for (const extra of [{ apiKey: ' ' }, { secretKey: ' ' }, { baseUrl: `${sink.url}/path` }, { baseUrl: 'http://user:password@localhost' }, { timeoutMillis: 0 }, { timeoutMillis: NaN }]) {
      assert.throws(() => configure(settings(sink, extra)));
    }
    const count = process.listenerCount('beforeExit');
    const native = instrument(createOpenTelemetryInstrumentation({ content: false }));
    try { assert.throws(() => configure(settings(sink)), /already/i); }
    finally { await native(); }
    assert.equal(process.listenerCount('beforeExit'), count);
    const handle = configure(settings(sink));
    try { assert.throws(() => configure(settings(sink, { projectName: 'other' })), /already configured/); }
    finally { await handle.shutdown(); }
  } finally { await sink.close(); }
});

test('background export failure is diagnosed without breaking execution', async () => {
  const sink = await recorder({ hang: true });
  const messages = [];
  diag.setLogger({ warn: (...args) => messages.push(args.join(' ')), error() {}, info() {}, debug() {}, verbose() {} });
  const handle = configure(settings(sink, { timeoutMillis: 100 }));
  try {
    const fixture = events(); fixture.start(); fixture.end();
    assert.doesNotThrow(() => fixture.emit({ type: 'submission_settled' }));
    await waitFor(() => messages.some((message) => message.includes('could not finish exporting')));
    assert.doesNotMatch(messages.join(' '), /synthetic-api|synthetic-secret/);
  } finally { await handle.shutdown().catch(() => {}); await sink.close(); diag.disable(); }
});

test('unresponsive collector bounds explicit flush and shutdown', async () => {
  const sink = await recorder({ hang: true });
  const handle = configure(settings(sink, { timeoutMillis: 100 }));
  try {
    const fixture = events(); fixture.start(); fixture.end();
    const began = Date.now();
    await assert.rejects(handle.flush());
    await handle.shutdown().catch(() => {});
    assert.ok(Date.now() - began < 1500);
  } finally { await handle.shutdown().catch(() => {}); await sink.close(); }
});
