/** Vitest 4 reporter for normalized vitest-evals harness traces. */
import { randomBytes } from 'node:crypto';
import { SpanKind, SpanStatusCode, type Attributes, type HrTime, type SpanContext } from '@opentelemetry/api';
import { ExportResultCode } from '@opentelemetry/core';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace-base';

// Same rule as @traceai/fi-core: FI_BASE_URL is a base URL, so one trailing slash is dropped and the
// collector path appended. An explicit endpoint is a full URL and wins over the environment.
const DEFAULT_BASE_URL = 'https://api.futureagi.com';
const COLLECTOR_PATH = '/tracer/v1/traces';
function collectorUrl(endpoint?: string): string {
  return endpoint ?? `${(process.env.FI_BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, '')}${COLLECTOR_PATH}`;
}
// Future AGI files every span under its resource's project; a batch without project_name is refused.
const DEFAULT_PROJECT = 'vitest-evals';
function projectResource(projectName?: string) {
  const name = projectName || process.env.FI_PROJECT_NAME || DEFAULT_PROJECT;
  return resourceFromAttributes({ 'service.name': 'vitest-evals', project_name: name, project_type: 'observe' });
}
const SCOPE = { name: '@traceai/vitest-evals', version: '0.1.1' };
const SAFE_KEYS = new Set([
  'gen_ai.system', 'gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.response.model',
  'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'gen_ai.usage.total_tokens',
]);
// The harness's own name for a root span that is renamed after its test.
const ORIGINAL_NAME = 'vitest.evals.original_name';

type Score = { name?: string; score?: number | null };
type SourceSpan = {
  id?: string; traceId?: string; parentId?: string; name: string; kind?: string;
  startedAt?: string; finishedAt?: string; durationMs?: number; status?: string;
  attributes?: Record<string, unknown>;
};
type SourceTrace = { id?: string; spans: SourceSpan[] };
type EvalMeta = { scores?: Score[]; avgScore?: number | null; thresholdFailed?: boolean };
type Run = { traces?: SourceTrace[]; usage?: { provider?: string; model?: string; inputTokens?: number; outputTokens?: number; totalTokens?: number } };
type TestLike = { name: string; fullName?: string; meta(): { eval?: EvalMeta; harness?: { run?: Run } }; result(): { state: string } };
type ModuleLike = { children: { allTests(): Iterable<TestLike> } };
export type ReporterOptions = { exporter?: SpanExporter; endpoint?: string; apiKey?: string; secretKey?: string; projectName?: string };

const hexId = (value: string | undefined, length: number): value is string =>
  typeof value === 'string' && new RegExp(`^[a-fA-F0-9]{${length}}$`).test(value) && !/^0+$/.test(value);
const newId = (bytes: number): string => randomBytes(bytes).toString('hex');
const hrtime = (ms: number): HrTime => [Math.floor(ms / 1000), Math.round((ms % 1000) * 1e6)];
const numeric = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
function times(span: SourceSpan): { start: HrTime; end: HrTime; duration: HrTime } {
  const startMs = span.startedAt ? Date.parse(span.startedAt) : NaN;
  const endMs = span.finishedAt ? Date.parse(span.finishedAt) : NaN;
  // Don't forge elapsed time for untimed spans; timestamp them at export time.
  const start = Number.isFinite(startMs) ? startMs :
    Number.isFinite(endMs) && numeric(span.durationMs) && span.durationMs >= 0 ? endMs - span.durationMs : Date.now();
  const end = Number.isFinite(endMs) && endMs >= start ? endMs :
    numeric(span.durationMs) && span.durationMs >= 0 ? start + span.durationMs : start;
  return { start: hrtime(start), end: hrtime(end), duration: hrtime(end - start) };
}
function safeAttributes(span: SourceSpan, evalMeta?: EvalMeta, run?: Run, originalName?: string): Attributes {
  const attributes: Attributes = { 'vitest.evals.kind': span.kind || 'custom' };
  // Set only by the reporter for a renamed root; never copied from the harness's own attributes.
  if (originalName) attributes[ORIGINAL_NAME] = originalName;
  for (const [key, value] of Object.entries(span.attributes || {})) {
    if (SAFE_KEYS.has(key) && (typeof value === 'string' && value.length <= 256 || numeric(value))) attributes[key] = value;
  }
  if (run) {
    if (run.usage?.provider) attributes['gen_ai.provider.name'] = run.usage.provider.slice(0, 128);
    if (run.usage?.model) attributes['gen_ai.request.model'] = run.usage.model.slice(0, 128);
    for (const [field, name] of [['inputTokens', 'input_tokens'], ['outputTokens', 'output_tokens'], ['totalTokens', 'total_tokens']] as const) {
      const value = run.usage?.[field];
      if (numeric(value) && value >= 0) attributes[`gen_ai.usage.${name}`] = value;
    }
  }
  if (evalMeta) {
    for (const [index, score] of (evalMeta.scores || []).entries()) {
      // Only simple score identifiers can become attribute keys; redact other names.
      const name = score.name && /^[a-zA-Z][a-zA-Z0-9_-]{0,47}$/.test(score.name) ? score.name : String(index);
      const key = `eval.score.${name}`;
      if (numeric(score.score)) attributes[key in attributes ? `eval.score.${index}` : key] = score.score;
    }
    if (numeric(evalMeta.avgScore)) attributes['eval.score.average'] = evalMeta.avgScore;
    if (evalMeta.thresholdFailed !== undefined) attributes['eval.threshold_failed'] = evalMeta.thresholdFailed;
  }
  return attributes;
}
function mappedTrace(trace: SourceTrace, resource: ReadableSpan['resource'], rootName: string, evalMeta?: EvalMeta, run?: Run): ReadableSpan[] {
  const sourceTraceId = trace.id ?? trace.spans.find(span => hexId(span.traceId, 32))?.traceId;
  const traceId = hexId(sourceTraceId, 32) ? sourceTraceId.toLowerCase() : newId(16);
  const ids = new Map<string, string>();
  const allocated = new Set<string>();
  const positions: string[] = [];
  for (const span of trace.spans) {
    let id = hexId(span.id, 16) ? span.id.toLowerCase() : newId(8);
    while (allocated.has(id)) id = newId(8);
    allocated.add(id);
    positions.push(id);
    if (span.id && !ids.has(span.id)) ids.set(span.id, id);
  }
  const parents = trace.spans.map((span, index) => {
    const parentId = span.parentId && ids.get(span.parentId);
    return parentId && parentId !== positions[index] ? parentId : undefined;
  });
  // Observe lists a trace by its root, so the first span without a parent is named after the test.
  const rootIndex = parents.findIndex(parentId => !parentId);
  return trace.spans.map((span, index) => {
    const originalId = span.id;
    // Allocation order is stable even for malformed duplicate source ids.
    const spanId = positions[index];
    const context: SpanContext = { traceId, spanId, traceFlags: 1 };
    const parentId = parents[index];
    const sourceName = span.name.slice(0, 256);
    // A test with no name keeps the harness's own root name.
    const named = index === rootIndex && rootName !== '';
    const renamed = named && sourceName !== rootName;
    const { start, end, duration } = times(span);
    return {
      name: named ? rootName : sourceName, kind: SpanKind.INTERNAL,
      spanContext: () => context,
      ...(parentId ? { parentSpanContext: { traceId, spanId: parentId, traceFlags: 1 } } : {}),
      startTime: start, endTime: end, duration, ended: true,
      status: { code: span.status === 'error' ? SpanStatusCode.ERROR : SpanStatusCode.OK },
      attributes: safeAttributes(span, index === 0 ? evalMeta : undefined, index === 0 ? run : undefined, renamed ? sourceName : undefined),
      events: [], links: [], resource, instrumentationScope: SCOPE,
      droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
    };
  });
}

export class VitestEvalsReporter {
  private readonly exporter: SpanExporter;
  private readonly resource: ReadableSpan['resource'];
  constructor(options: ReporterOptions = {}) {
    this.resource = projectResource(options.projectName);
    this.exporter = options.exporter ?? new OTLPTraceExporter({
      url: collectorUrl(options.endpoint),
      headers: {
        'x-api-key': options.apiKey ?? process.env.FI_API_KEY ?? '',
        'x-secret-key': options.secretKey ?? process.env.FI_SECRET_KEY ?? '',
      },
    });
  }
  async onTestRunEnd(modules: ReadonlyArray<ModuleLike>): Promise<void> {
    const spans: ReadableSpan[] = [];
    for (const module of modules) for (const test of module.children.allTests()) {
      const { eval: evalMeta, harness } = test.meta();
      if (!evalMeta && !harness) continue;
      if (test.result().state === 'skipped' || test.result().state === 'pending') continue;
      // Vitest's full name includes the enclosing describe blocks; older or custom test objects may only have a name.
      const testName = test.fullName || test.name;
      const run = harness?.run;
      const traces = run?.traces?.filter(trace => trace.spans?.length) ?? [];
      if (!traces.length) traces.push({ spans: [{ name: testName, kind: 'run', status: test.result().state === 'failed' ? 'error' : 'ok' }] });
      for (const [index, trace] of traces.entries()) {
        // Several traces from one test are numbered so their roots stay distinguishable.
        const suffix = traces.length > 1 ? ` (${index + 1}/${traces.length})` : '';
        const rootName = testName ? testName.slice(0, 256 - suffix.length) + suffix : '';
        spans.push(...mappedTrace(trace, this.resource, rootName, index === 0 ? evalMeta : undefined, index === 0 ? run : undefined));
      }
    }
    if (!spans.length) return;
    await new Promise<void>((resolve, reject) => this.exporter.export(spans, result => {
      if (result.code === ExportResultCode.SUCCESS) resolve();
      else reject(result.error ?? new Error('OTLP trace export failed'));
    }));
  }
  async onClose(): Promise<void> { await this.exporter.shutdown(); }
}
export default VitestEvalsReporter;
