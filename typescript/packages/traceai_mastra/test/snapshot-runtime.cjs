/* Actual workflow Date regression; the parent Jest suite owns the heavy.sh slot. */
const assert = require('node:assert/strict');
const [scenario, format = 'cjs'] = process.argv.slice(2);
const load = name => format === 'esm' ? import(name) : Promise.resolve(require(name));
const baseline = scenario === 'snapshot-baseline';
const spans = [];

async function main() {
  // No provider/collector traffic, including accidentally introduced requests.
  globalThis.fetch = () => { throw new Error('Unexpected network call'); };
  let auto;
  if (!baseline) {
    auto = require('@traceai/auto');
    auto.init({ apiKey: 'test-fi-key', secretKey: 'test-fi-secret', projectName: 'mastra-snapshot-regression',
      instrumentations: ['mastra', 'wire'], wireCapture: true,
      exporter: { export(batch, callback) { spans.push(...batch); callback({ code: 0 }); }, async shutdown() {}, async forceFlush() {} },
    });
    assert.equal(auto.report().active, true);
    assert.equal(auto.report().integrations.find(item => item.name === 'mastra').status, 'instrumented');
  }
  const { Mastra } = await load('@mastra/core');
  const { createWorkflow, createStep } = await load('@mastra/core/workflows');
  const { Observability, BaseExporter } = await load('@mastra/observability');
  const { z } = require('zod');
  const appEvents = [];
  class AppExporter extends BaseExporter {
    name = 'app';
    async _exportTracingEvent(event) { appEvents.push(event); }
    async flush() {}
    async shutdown() {}
  }
  const observability = new Observability({ configs: { app: { serviceName: 'app', exporters: [new AppExporter()] } }, sensitiveDataFilter: false });
  const completedAt = new Date('2020-01-02T03:04:05.000Z');
  let toJSONCalls = 0;
  completedAt.toJSON = function () { toJSONCalls++; this.setUTCFullYear(2099); return Date.prototype.toISOString.call(this); };
  const outputSchema = z.object({ completedAt: z.date() });
  const step = createStep({ id: 'date-step', inputSchema: z.object({}), outputSchema, execute: async () => ({ completedAt }) });
  const workflow = createWorkflow({ id: 'date-workflow', inputSchema: z.object({}), outputSchema }).then(step).commit();
  const mastra = new Mastra({ logger: false, observability, workflows: { workflow } });
  const run = await mastra.getWorkflow('workflow').createRun();
  const result = await run.start({ inputData: {} });
  assert.equal(result.status, 'success');
  await mastra.observability.shutdown();
  if (auto) await globalThis[Symbol.for('traceai.auto.agent.v1')].processor.forceFlush();
  const output = { status: result.status, toJSONCalls, resultYear: result.result.completedAt.getUTCFullYear(),
    sameObject: result.result.completedAt === completedAt, epoch: Date.prototype.getTime.call(result.result.completedAt) };
  assert.equal(output.toJSONCalls, 0, 'export must never call an application Date.toJSON');
  assert.equal(output.resultYear, 2020, 'instrumentation must not mutate workflow results');
  assert.equal(output.sameObject, true, 'Mastra retains the original Date identity');
  const event = appEvents.find(event => event.type === 'span_ended' && event.exportedSpan.output?.completedAt);
  assert(event, 'the preserved application exporter sees the original workflow snapshot');
  assert.equal(event.exportedSpan.output.completedAt, completedAt, 'do not rewrite another exporter’s snapshot');
  assert.equal(event.exportedSpan.output.completedAt.toJSON, completedAt.toJSON, 'do not remove application methods');
  if (auto) {
    const captured = spans.filter(span => span.attributes['output.value']?.includes('2020-01-02T03:04:05.000Z'));
    assert(captured.length >= 2, 'both real step and workflow spans retain output content by default');
    assert(captured.every(span => JSON.parse(span.attributes['output.value']).completedAt === '2020-01-02T03:04:05.000Z'));
    await auto.shutdown();
  } else assert.equal(spans.length, 0);
  console.log(JSON.stringify({ ok: true, output: JSON.stringify(output), spanCount: spans.length }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
