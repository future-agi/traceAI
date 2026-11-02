const assert = require('node:assert/strict');
const http = require('node:http');
const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor, InMemorySpanExporter } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');

exports.start = async () => {
  const requests = [];
  const memory = new InMemorySpanExporter();
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString();
      requests.push({ headers: req.headers, path: req.url, body: JSON.parse(raw) });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{}');
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(memory),
    new SimpleSpanProcessor(new OTLPTraceExporter({
      url: `http://127.0.0.1:${server.address().port}/tracer/v1/traces`,
      headers: { FI_API_KEY: 'synthetic-fi-api', FI_SECRET_KEY: 'synthetic-fi-secret' },
    })),
  ] });
  provider.register();
  const wire = () => requests.flatMap(r => r.body.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(ss => ss.spans)));
  return {
    provider, memory, requests, wire,
    flush: () => provider.forceFlush(),
    close: async () => {
      await provider.forceFlush();
      for (const r of requests) {
        assert.equal(r.path, '/tracer/v1/traces');
        assert.equal(r.headers.fi_api_key, 'synthetic-fi-api');
        assert.equal(r.headers.fi_secret_key, 'synthetic-fi-secret');
        assert.match(r.headers['content-type'], /json/);
        assert.ok(r.body.resourceSpans[0].resource);
      }
      for (const span of wire()) {
        assert.match(span.traceId, /^[0-9a-f]{32}$/);
        assert.match(span.spanId, /^[0-9a-f]{16}$/);
        assert.ok(BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano));
      }
      await provider.shutdown();
      await new Promise(resolve => server.close(resolve));
    },
  };
};
exports.attributes = span => Object.fromEntries((span.attributes || []).map(a => [a.key,
  a.value.stringValue ?? a.value.intValue ?? a.value.boolValue ?? a.value.arrayValue]));
