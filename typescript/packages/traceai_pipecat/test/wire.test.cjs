const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { gunzipSync } = require('node:zlib');
const { BasicTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { Resource } = require('@opentelemetry/resources');
const { OTLPTraceExporter: JsonExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { OTLPTraceExporter: ProtoExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { opentelemetry } = require('@opentelemetry/otlp-transformer/build/src/generated/root');
const { PipecatInstrumentation, observePipecatClient } = require('../dist/src/index.js');
const { createClient } = require('./transport.cjs');

function attributes(list) {
  return Object.fromEntries(list.map(({ key, value }) => [key, value.stringValue]));
}

async function receiver(t) {
  const received = [];
  const server = http.createServer((req, res) => {
    const parts = [];
    req.on('data', part => parts.push(part));
    req.on('end', () => {
      try {
        let bytes = Buffer.concat(parts);
        if (req.headers['content-encoding'] === 'gzip') bytes = gunzipSync(bytes);
        const isJson = req.headers['content-type'] === 'application/json';
        const decoded = isJson ? JSON.parse(bytes.toString()) : opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest.decode(bytes);
        received.push({ decoded, headers: req.headers, url: req.url });
        res.writeHead(200, { 'content-type': isJson ? 'application/json' : 'application/x-protobuf' });
        res.end(isJson ? '{}' : Buffer.alloc(0));
      } catch (error) { res.writeHead(400); res.end(String(error)); }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeIdleConnections(); }));
  return { received, url: `http://127.0.0.1:${server.address().port}/v1/traces` };
}

for (const name of ['pipecat-floor', '@pipecat-ai/client-js']) {
  for (const [format, Exporter] of [['json', JsonExporter], ['protobuf', ProtoExporter]]) {
    for (const compression of ['none', 'gzip']) {
      test(`${name}: ${format}/${compression} wire shape and privacy`, { timeout: 10000 }, async t => {
        const { url, received } = await receiver(t);
        const provider = new BasicTracerProvider({
          resource: new Resource({ project_name: 'pipecat-client', project_type: 'observe' }),
          spanProcessors: [new SimpleSpanProcessor(new Exporter({ url, compression }))],
        });
        const inst = new PipecatInstrumentation();
        inst.setTracerProvider(provider);
        const sdk = require(name);
        inst.manuallyInstrument(sdk);
        const { client, transport } = createClient(sdk);
        const stop = observePipecatClient(client, { tracer: provider.getTracer('events') });
        t.after(async () => { stop(); inst.disable(); await provider.shutdown(); });
        await client.connect();
        if (sdk.PipecatClient) await client.sendText('synthetic-private-transcript');
        else client.sendMessage(new sdk.RTVIMessage('synthetic-private-type', { text: 'synthetic-private-transcript' }));
        transport.onMessage(new sdk.RTVIMessage('error', { error: 'synthetic-private-token' }));
        await client.disconnect();
        await provider.forceFlush();
        assert.ok(received.length > 0);
        assert.ok(received.every(r => r.url === '/v1/traces'));
        assert.ok(received.every(r => compression === 'gzip' ? r.headers['content-encoding'] === 'gzip' : !r.headers['content-encoding']));
        const resources = received.flatMap(r => r.decoded.resourceSpans);
        assert.ok(resources.every(r => attributes(r.resource.attributes).project_name === 'pipecat-client'));
        const spans = resources.flatMap(r => r.scopeSpans.flatMap(s => s.spans));
        assert.equal(spans.filter(s => attributes(s.attributes)['pipecat.operation'] === 'client.connect').length, 1);
        assert.equal(spans.filter(s => attributes(s.attributes)['pipecat.event'] === 'botReady').length, 1);
        assert.ok(spans.every(s => attributes(s.attributes)['fi.span.kind'] === 'CHAIN'));
        assert.ok(spans.every(s => attributes(s.attributes)['pipecat.telemetry_source'] === 'client'));
        assert.ok(spans.every(s => Number(s.endTimeUnixNano) >= Number(s.startTimeUnixNano)));
        assert.ok(!JSON.stringify(received).includes('synthetic-private'));
      });
    }
  }
}

module.exports = { receiver };
