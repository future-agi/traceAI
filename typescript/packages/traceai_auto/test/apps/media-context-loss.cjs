const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { trace } = require('@opentelemetry/api');
const coreRequire = createRequire(require.resolve('@traceai/fi-core'));
function sdk() {
  if (process.env.SDK === 'core') return coreRequire('@opentelemetry/sdk-trace-base');
  if (process.env.SDK !== 'legacy') return require('@opentelemetry/sdk-trace-base');
  try { return require('otel-sdk-trace-base-legacy'); }
  catch {
    // As in defects.cjs: lockfile-only installs can leave the existing link absent.
    const { readdirSync } = require('node:fs'), { resolve, join } = require('node:path');
    const store = resolve(__dirname, '../../../../node_modules/.pnpm');
    const directory = readdirSync(store).find(name => name.startsWith('@opentelemetry+sdk-trace-base@1.30.1_'));
    assert(directory, 'Real OTel 1.30.1 SDK must be installed');
    return require(join(store, directory, 'node_modules/@opentelemetry/sdk-trace-base'));
  }
}
const { BasicTracerProvider, SimpleSpanProcessor } = sdk();
const core = require('@traceai/fi-core');
const genkitRequire = createRequire(require('node:path').resolve(__dirname, '../../../traceai_genkit/package.json'));
const native = [], beforeExport = [], nativeBefore = [], nativeWitnesses = [];
const nanos = time => String(BigInt(time[0]) * 1000000000n + BigInt(time[1]));
const scopeOf = span => span.instrumentationScope ?? span.instrumentationLibrary ?? {};
const stamp = span => JSON.stringify({ context: span.spanContext(), name: span.name, attributes: span.attributes, status: span.status, events: span.events, links: span.links, resource: span.resource.attributes, scope: scopeOf(span), startTime: span.startTime, endTime: span.endTime, droppedAttributesCount: span.droppedAttributesCount, droppedEventsCount: span.droppedEventsCount, droppedLinksCount: span.droppedLinksCount });
function witness(span) {
  const atLimit = value => typeof value === 'string' ? valueLimit > 0 && value.length >= valueLimit : Array.isArray(value) && value.some(atLimit);
  const events = span.events || [], links = span.links || [], scope = scopeOf(span);
  const surfaces = [span.attributes, span.resource.attributes, scope.attributes, ...events.map(event => event.attributes), ...links.map(link => link.attributes)];
  return { traceId: span.spanContext().traceId, spanId: span.spanContext().spanId, name: span.name,
    hostCountLoss: span.droppedAttributesCount > 0 || span.droppedEventsCount > 0 || span.droppedLinksCount > 0 || [span.resource, scope, ...events, ...links].some(item => item?.droppedAttributesCount > 0),
    valueAtLimit: Boolean(surfaces.some(attrs => Object.values(attrs || {}).some(atLimit)) || atLimit(span.status.message)),
    droppedAttributesCount: span.droppedAttributesCount || 0, droppedEventsCount: span.droppedEventsCount || 0, droppedLinksCount: span.droppedLinksCount || 0,
    startTimeUnixNano: nanos(span.startTime), endTimeUnixNano: nanos(span.endTime),
    events: events.map(event => ({ timeUnixNano: nanos(event.time), droppedAttributesCount: event.droppedAttributesCount || 0, attributeKeys: Object.keys(event.attributes || {}) })),
    links: links.map(link => ({ traceId: link.context.traceId, spanId: link.context.spanId, droppedAttributesCount: link.droppedAttributesCount || 0, attributeKeys: Object.keys(link.attributes || {}) })) };
}
const nativeProcessor = new SimpleSpanProcessor({ export(spans, done) { native.push(...spans); beforeExport.push(...spans.map(s => JSON.stringify(s.attributes))); nativeBefore.push(...spans.map(stamp)); nativeWitnesses.push(...spans.map(witness)); done({ code: 0 }); }, async shutdown() {} });
const valueLimit = Number(process.env.VALUE || 0), count = Number(process.env.COUNT || 128);
const manual = process.env.ENTRY === 'manual';
let host, fi;
if (manual) fi = core.register({ setGlobalTracerProvider: false, verbose: false, endpoint: process.env.FI_BASE_URL });
if (process.env.OWNER === 'host' || manual) {
  const limits = { attributeCountLimit: count, ...(valueLimit ? { attributeValueLengthLimit: valueLimit } : {}) };
  const processors = [nativeProcessor, ...(manual ? [new (genkitRequire('@traceai/genkit').FIGenkitSpanProcessor)({ tracerProvider: fi })] : [])];
  host = new BasicTracerProvider({ [process.env.LIMIT_SOURCE === 'general' ? 'generalLimits' : 'spanLimits']: limits,
    spanProcessors: process.env.SDK === 'legacy' ? [] : processors });
  if (process.env.SDK === 'legacy') for (const processor of processors) host.addSpanProcessor(processor);
  trace.setGlobalTracerProvider(host);
}
const early = trace.getTracer('preexisting-application');

async function main() {
  const auto = manual ? undefined : require('@traceai/auto');
  if (process.env.ENTRY === 'init') assert(auto.init({ exporter: core.createSpanExporter({ endpoint: process.env.FI_BASE_URL, verbose: false }) }));
  if (auto) assert(auto.report().active);
  if (!host) {
    const proxy = trace.getTracerProvider(), provider = proxy.getDelegate?.() || proxy;
    const processors = provider._activeSpanProcessor?._spanProcessors;
    assert(Array.isArray(processors), 'owned native observer requires the existing live SDK processor list');
    assert(!processors.includes(nativeProcessor)); processors.unshift(nativeProcessor);
  }
  const payload = 'AQID'.repeat(16);
  if (process.env.SCENARIO === 'genkit-rewritten') {
    const { genkit, z } = genkitRequire('genkit');
    const { homedir, userInfo } = require('node:os');
    const ai = genkit({});
    const url = `https://example.invalid/u?data=${payload.repeat(8)}&mimeType=audio/wav`;
    for (const [label, prefix] of [['home', homedir() + '/clip.wav '], ['cwd', process.cwd() + '/clip.wav ']]) {
      const output = prefix + url;
      const flow = ai.defineFlow({ name: `rewritten-output-${label}`, inputSchema: z.any(), outputSchema: z.any() }, async () => output);
      assert.equal(await flow(null), output);
    }
    for (const [label, prefix] of [['user', userInfo().username + ': '], ['cwd', process.cwd() + '/clip.wav '], ['email', 'synthetic.person@example.invalid: ']]) {
      const error = new Error(prefix + url);
      const flow = ai.defineFlow({ name: `rewritten-error-${label}`, inputSchema: z.any(), outputSchema: z.any() }, async () => { throw error; });
      await assert.rejects(flow(null), caught => caught === error);
    }
  } else if (process.env.SCENARIO === 'genkit') {
    const { genkit, z } = genkitRequire('genkit');
    const ai = genkit({});
    const flow = ai.defineFlow({ name: 'mediaContextFlow', inputSchema: z.any(), outputSchema: z.any() }, async input => input);
    const input = { file: { data: payload.repeat(4), mimeType: 'audio/wav' } };
    const result = await flow(input);
    assert.deepEqual(result, input);
    // A native Genkit span with a flat MIME discriminator proves that the agent
    // retained the host's code-only limit even for balanced/non-JSON values.
    const span = trace.getTracer('genkit-tracer').startSpan('mapped-limit-evidence', { attributes: {
      'genkit:type': 'flow', 'custom.parts': JSON.stringify([{ data: payload }]),
      // This MIME looks non-media after truncation, but never was complete.
      'custom.mimeType': 'application/' + 'z'.repeat(Math.max(1, valueLimit - 12)) + '; source=fixture',
    } });
    span.end();
  } else {
    const tracer = host ? early : trace.getTracer('application');
    const ctx = { traceId: '1'.repeat(32), spanId: '2'.repeat(16), traceFlags: 1 };
    for (const boundary of ['', 'transcript.', 'id.']) {
      const attrs = valueLimit ? { 'error.message': `Bad request: {"${boundary}inlineData":{"data":"${payload}","mimeType":"audio/wav"}}` }
        : { 'x.parts': JSON.stringify([{ [`${boundary}data`]: payload }]),
          ...Object.fromEntries(Array.from({ length: count - 1 }, (_, i) => [`filler.${i}`, i])), 'x.mimeType': 'audio/wav' };
      const before = JSON.stringify(attrs);
      const span = tracer.startSpan(`media-context-${boundary}`, { attributes: attrs, links: [{ context: ctx, attributes: attrs }] });
      span.addEvent('exception', valueLimit ? { 'exception.message': attrs['error.message'] } : attrs);
      span.end();
      assert.equal(JSON.stringify(attrs), before);
    }
  }
  if (auto) await auto.forceFlush(); else await fi.forceFlush();
  if (host) {
    assert(native.some(s => JSON.stringify(s.attributes).includes('AQIDAQID')));
    assert.deepEqual(native.map(s => JSON.stringify(s.attributes)), beforeExport);
    if (!process.env.SCENARIO.startsWith('genkit') && valueLimit) {
      assert(native.every(s => s.events[0].attributes['exception.message'].length === valueLimit));
      assert(native.every(s => s.links[0].attributes['error.message'].length === valueLimit));
    }
  }
  assert.deepEqual(native.map(stamp), nativeBefore);
  const expectedNames = process.env.SCENARIO === 'genkit-rewritten' ? ['output-home','output-cwd','error-user','error-cwd','error-email'].map(name => `rewritten-${name}`) : process.env.SCENARIO === 'genkit' ? ['mediaContextFlow','mapped-limit-evidence'] : [];
  const lineages = expectedNames.map(name => { const matches = nativeWitnesses.filter(item => item.name === name); assert.equal(matches.length, 1); return matches[0]; });
  console.log(JSON.stringify({ parity: true, nativeUnchanged: true, lineages }));
  if (auto) await auto.shutdown(); else await fi.shutdown();
  if (host) await host.shutdown();
}
module.exports = { main };
