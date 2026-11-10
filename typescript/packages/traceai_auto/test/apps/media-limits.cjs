const assert = require('node:assert/strict');
const { trace } = require('@opentelemetry/api');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { FITracer } = require('@traceai/fi-core');
const count = Number(process.env.COUNT || 128), valueLimit = Number(process.env.VALUE || 0);
const native = [];
// This file is also the host preload, before the real zero-code register entry.
let host;
if (process.env.OWNER === 'host') {
  host = new BasicTracerProvider({ spanLimits: { attributeCountLimit: count, attributePerEventCountLimit: count, attributePerLinkCountLimit: count,
    ...(valueLimit ? { attributeValueLengthLimit: valueLimit } : {}) },
    spanProcessors: [new SimpleSpanProcessor({ export(spans, done) { native.push(...spans); done({ code: 0 }); }, async shutdown() {} })] });
  trace.setGlobalTracerProvider(host);
}
const earlyTracer = trace.getTracer('preexisting-application');
async function main() {
  const auto = require('@traceai/auto');
  if (process.env.ENTRY === 'init') assert(auto.init());
  assert(auto.report().active);
  const tracer = process.env.OWNER === 'host' ? earlyTracer : trace.getTracer('application');
  const ctx = { traceId: '1'.repeat(32), spanId: '2'.repeat(16), traceFlags: 1 };
  let spans = 0;
  const lineages = [], nativeCopies = [];
  const nanos = time => String(BigInt(time[0]) * 1000000000n + BigInt(time[1]));
  const atLimit = value => typeof value === 'string' ? valueLimit > 0 && value.length >= valueLimit : Array.isArray(value) && value.some(atLimit);
  const stamp = span => JSON.stringify({ context: span.spanContext(), name: span.name, attributes: span.attributes, status: span.status, events: span.events, links: span.links, resource: span.resource.attributes, scope: span.instrumentationScope, startTime: span.startTime, endTime: span.endTime, droppedAttributesCount: span.droppedAttributesCount, droppedEventsCount: span.droppedEventsCount, droppedLinksCount: span.droppedLinksCount });
  const witness = (span, wrapped) => {
    const events = span.events || [], links = span.links || [];
    const surfaces = [span.attributes, span.resource.attributes, span.instrumentationScope.attributes, ...events.map(event => event.attributes), ...links.map(link => link.attributes)];
    const hostCountLoss = (!wrapped && span.droppedAttributesCount > 0) || span.droppedEventsCount > 0 || span.droppedLinksCount > 0 || [...events, ...links].some(item => item.droppedAttributesCount > 0);
    const valueAtLimit = surfaces.some(attrs => Object.values(attrs || {}).some(atLimit)) || atLimit(span.status.message);
    return { traceId: span.spanContext().traceId, spanId: span.spanContext().spanId, name: span.name, hostCountLoss, valueAtLimit: Boolean(valueAtLimit),
      exactNativeKeys: true, attributeKeys: Object.keys(span.attributes), resourceKeys: Object.keys(span.resource.attributes), scopeKeys: Object.keys(span.instrumentationScope.attributes || {}), statusCode: span.status.code, statusHasMessage: span.status.message !== undefined,
      droppedAttributesCount: span.droppedAttributesCount || 0, droppedEventsCount: span.droppedEventsCount || 0, droppedLinksCount: span.droppedLinksCount || 0,
      startTimeUnixNano: nanos(span.startTime), endTimeUnixNano: nanos(span.endTime),
      events: events.map(event => ({ timeUnixNano: nanos(event.time), droppedAttributesCount: event.droppedAttributesCount || 0, attributeKeys: Object.keys(event.attributes || {}) })),
      links: links.map(link => ({ traceId: link.context.traceId, spanId: link.context.spanId, droppedAttributesCount: link.droppedAttributesCount || 0, attributeKeys: Object.keys(link.attributes || {}) })) };
  };
  for (const wrapped of [false, true]) {
    const active = wrapped ? new FITracer({ tracer }) : tracer;
    for (const boundary of ['', '.transcript', '.id']) for (const hint of ['mimeType', 'mime_type', 'media_type', 'type', 'format']) {
      const parent = `gen_ai.input.messages.159.parts[0]${boundary}${hint === 'format' ? '.input_audio' : '.inlineData'}`;
      const attributes = { [`${parent}.data[0]`]: ['AQID'],
        ...(!valueLimit ? Object.fromEntries(Array.from({ length: count + 20 }, (_, i) => [`gen_ai.input.messages.${i}.content`, 'ordinary words'])) : {}),
        [`${parent}.${hint}`]: hint === 'type' ? 'input_audio' : hint === 'format' ? 'pcm' : 'application/octet-stream; encoding=binary' };
      const before = JSON.stringify(attributes);
      const span = active.startSpan(`media-${wrapped}-${boundary}-${hint}`, { attributes, links: [{ context: ctx, attributes }] });
      assert.equal(span.setAttributes(attributes), span);
      assert.equal(span.addEvent('event', attributes), span);
      assert.equal(span.addLink({ context: ctx, attributes }), span);
      assert.equal(span.addLinks([{ context: ctx, attributes }]), span);
      span.end(); spans++;
      const raw = wrapped ? Object.getOwnPropertyDescriptor(span, 'span')?.value : span;
      assert(raw && raw.attributes && typeof raw.spanContext === 'function', 'bound standalone FISpan native slot must exist');
      assert.deepEqual(raw.spanContext(), span.spanContext());
      lineages.push(witness(raw, wrapped)); nativeCopies.push({ raw, before: stamp(raw) });
      assert.equal(JSON.stringify(attributes), before);
    }
  }
  // Real request/response parity: instrumentation may sanitize its copy only.
  const request = JSON.stringify({ model: 'fixture-model', messages: Array.from({ length: 160 }, (_, i) => ({ role: 'user', content: i === 159 ? [{ type: 'input_audio', input_audio: { data: 'AQID', format: 'pcm' } }] : 'ordinary words' })) });
  const response = await fetch(process.env.FI_BASE_URL + '/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: request });
  assert.equal(response.status, 200); assert.equal(response.headers.get('x-parity'), 'unchanged');
  const body = await response.json(); assert.equal(body.received, request); assert.equal(body.choices[0].message.content, 'ordinary answer');
  await auto.forceFlush();
  if (host) {
    assert(native.some(span => span.name.startsWith('media-false-') && JSON.stringify(span.attributes).includes('AQID')));
    assert.equal(native.filter(span => span.name.startsWith('media-')).length, spans);
  }
  for (const item of nativeCopies) assert.equal(stamp(item.raw), item.before);
  console.log(JSON.stringify({ spans, parity: true, nativeUnchanged: !host || native.some(span => JSON.stringify(span.attributes).includes('AQID')), lineages }));
  await auto.shutdown(); if (host) await host.shutdown();
}
// --require loads only the host; the main invocation uses its cached module.
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
else module.exports = { main };
