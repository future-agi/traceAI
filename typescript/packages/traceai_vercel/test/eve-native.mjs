// Offline native-engine and OTLP wire contract. No model or vendor calls.
// argv: installed Eve package directory, compiled eve.js, OTel major (1 or 2).
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { readFile } from 'node:fs/promises';

const [eveDir, adapterFile, major = '1'] = process.argv.slice(2);
const { version } = JSON.parse(await readFile(join(eveDir, 'package.json'), 'utf8'));
const native = await import(pathToFileURL(join(eveDir, 'dist/src/tracing/eve/otel-declaration.js')));
const enginePath = version === '0.80.0' ? '@eve/tracing' : '@vercel/agent-runs';
const engine = await import(pathToFileURL(join(eveDir, `dist/src/compiled/${enginePath}/index.js`)));
const { createEveDestination } = await import(pathToFileURL(adapterFile));
const sdk = await import(major === '1' ? '@opentelemetry/sdk-trace-base' : 'otel2');
const { OTLPTraceExporter } = await import(major === '1' ? '@opentelemetry/exporter-trace-otlp-http' : 'otlp2');
const resources = await import(major === '1' ? '@opentelemetry/resources' : 'resources2');
const resource = major === '1'
  ? new resources.Resource({ project_name: 'eve-offline', project_type: 'observe' })
  : resources.resourceFromAttributes({ project_name: 'eve-offline', project_type: 'observe' });
const payloads = [];
const server = createServer(async (req, res) => {
  assert.equal(req.url, '/tracer/v1/traces');
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  payloads.push(JSON.parse(Buffer.concat(chunks).toString()));
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end('{}');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const url = `http://127.0.0.1:${server.address().port}/tracer/v1/traces`;
const attrs = span => Object.fromEntries(span.attributes.map(({ key, value }) => [key, value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue]));

try {
  for (const captureContent of [false, true]) {
    for (const ceiling of [false, true]) {
      payloads.length = 0;
      const exporter = new OTLPTraceExporter({ url });
      const destination = native.otelIntegration(createEveDestination({
        processor: new sdk.BatchSpanProcessor(exporter), captureContent,
      }));
      const otherExporter = new sdk.InMemorySpanExporter();
      const other = native.otelIntegration({ traceExporter: otherExporter });
      const pipeline = native.collectOtelPipeline([
        native.otel({ resource: { project_name: 'eve-offline', project_type: 'observe' } }),
        destination, other,
      ]);
      const processors = pipeline.configuration.spanProcessors;
      const provider = new sdk.BasicTracerProvider({ resource, ...(major === '2' ? { spanProcessors: processors } : {}) });
      if (major === '1') processors.forEach(processor => provider.addSpanProcessor(processor));
      const tracing = (engine.createAgentTracing ?? engine.createAgentRun)({
        telemetry: engine.otelTelemetry({ provider, tracerName: 'eve.agent' }),
        onError: error => { throw error; },
      });
      try {
        const turn = await tracing.turn({
          identity: { conversationId: 'conversation-1', runId: 'run-1', turnId: 'turn-1' },
          agentName: 'weather', sequence: 0,
          capture: { emit: true, recordInputs: ceiling, recordOutputs: ceiling },
          attributes: { 'agent.principal.current.id': 'PRIVATE_PRINCIPAL', 'agent.principal.initiator.id': 'PRIVATE_INITIATOR' },
        });
        const step = await turn.attempt({ stepIndex: 0, attempt: 0 });
        const model = await step.modelCall({
          modelId: 'synthetic-model', provider: 'synthetic-provider',
          messages: [{ role: 'user', content: 'PRIVATE_PROMPT' }], instructions: 'PRIVATE_SYSTEM',
          tools: [{ name: 'weather', description: 'PRIVATE_DEFINITION', inputSchema: { type: 'object' } }],
        });
        await model.complete({ model: { usage: { inputTokens: 12, outputTokens: 4 }, finishReason: 'stop', content: [{ type: 'text', text: 'PRIVATE_OUTPUT' }] } });
        const tool = await step.tool({ callId: 'call-1', name: 'weather', arguments: { city: 'PRIVATE_ARGUMENTS' } });
        await tool.complete({ output: { weather: 'PRIVATE_RESULT' } });
        const failure = await step.tool({ callId: 'call-error', name: 'failed_weather', arguments: {} });
        await failure.fail(new Error('PRIVATE_ERROR'));
        await step.complete();
        await turn.complete({ usage: { inputTokens: 12, outputTokens: 4 } });
        provider.getTracer('workflow').startSpan('plumbing').end();
        await provider.forceFlush();
        const wire = JSON.stringify(payloads);
        const spans = payloads.flatMap(payload => payload.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
        assert.equal(spans.length, 5, 'one agent, step, model and two tools; no plumbing or duplicate spans');
        const agent = spans.find(span => span.name === 'invoke_agent weather');
        const chat = spans.find(span => span.name === 'chat synthetic-model');
        const agentAttributes = attrs(agent);
        assert.equal(agentAttributes['fi.span.kind'], 'AGENT');
        assert.equal(agentAttributes['session.id'], 'conversation-1');
        assert.equal(agentAttributes['gen_ai.usage.input_tokens'], undefined);
        assert.equal(Number(agentAttributes['eve.aggregate.usage.input_tokens']), 12);
        assert.equal(Number(attrs(chat)['gen_ai.usage.input_tokens']), 12);
        assert.equal(spans.reduce((sum, span) => sum + Number(attrs(span)['gen_ai.usage.input_tokens'] ?? 0), 0), 12);
        assert.equal(attrs(spans.find(span => span.name === 'agent.step'))['fi.span.kind'], 'CHAIN');
        assert.ok(spans.filter(span => span.name.startsWith('execute_tool')).every(span => attrs(span)['fi.span.kind'] === 'TOOL'));
        assert.ok(spans.every(span => span.traceId === agent.traceId));
        const ids = new Set(spans.map(span => span.spanId));
        assert.ok(spans.every(span => !span.parentSpanId || ids.has(span.parentSpanId)), 'no dangling parent IDs');
        assert.ok(payloads.every(payload => payload.resourceSpans.every(resource => attrs(resource.resource).project_name === 'eve-offline')));
        assert.equal(wire.includes('PRIVATE_PRINCIPAL'), captureContent);
        for (const marker of ['PRIVATE_PROMPT', 'PRIVATE_SYSTEM', 'PRIVATE_OUTPUT', 'PRIVATE_ARGUMENTS', 'PRIVATE_RESULT', 'PRIVATE_ERROR']) {
          assert.equal(wire.includes(marker), captureContent && ceiling, `${marker}: capture=${captureContent} ceiling=${ceiling}`);
        }
        assert.equal(Boolean(attrs(chat)['input.value']), captureContent && ceiling);
        assert.equal(Boolean(attrs(chat)['output.value']), captureContent && ceiling);
        const others = otherExporter.getFinishedSpans();
        assert.equal(others.length, 6);
        assert.equal(others.find(span => span.name === 'invoke_agent weather').attributes['fi.span.kind'], undefined);
        assert.equal(others.find(span => span.name === 'invoke_agent weather').attributes['gen_ai.usage.input_tokens'], 12);
        assert.equal(JSON.stringify(others.map(span => span.attributes)).includes('PRIVATE_PROMPT'), ceiling, 'privacy remains per destination');
        console.log(`PASS eve=${version} otel=${major} capture=${captureContent} ceiling=${ceiling}: native producer, privacy, semantics, leaf usage and localhost OTLP`);
      } finally {
        await provider.shutdown();
      }
    }
  }
} finally {
  await new Promise(resolve => server.close(resolve));
}
