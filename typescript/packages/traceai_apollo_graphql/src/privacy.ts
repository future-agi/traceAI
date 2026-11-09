import type { Attributes } from '@opentelemetry/api';
import { ExportResultCode, type ExportResult } from '@opentelemetry/core';
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace';
import { generateTraceConfig, redactPiiInString, REDACTED_VALUE, type TraceConfig, type TraceConfigOptions } from '@traceai/fi-core';

export interface PrivacyOptions extends TraceConfigOptions {
  /**
   * Include upstream query source and error messages. configure() masks string and
   * numeric literals only; enum, Boolean and null literals remain visible.
   * Never includes stacks or variable-value attributes. Error messages may contain data.
   * Shared FI hiding/redaction controls still apply; captureContent cannot bypass them.
   * When wrapping your own exporter, source masking is your instrumentation's responsibility.
   */
  captureContent?: boolean;
}

const scopes = new Set([
  '@opentelemetry/instrumentation-graphql',
  '@opentelemetry/instrumentation-http',
]);
const metadata = new Set([
  'graphql.operation.type', 'graphql.operation.name', 'graphql.field.name',
  'graphql.field.path', 'graphql.field.type', 'graphql.parent.name',
  'http.request.method', 'http.response.status_code', 'http.method', 'http.status_code',
]);

// Operation names and response paths (including aliases) are query input, not schema metadata.
const inputIdentifiers = new Set(['graphql.operation.name', 'graphql.field.path']);
const fixedGraphQLNames = new Set(['graphql.parse', 'graphql.validate', 'graphql.parseSchema', 'graphql.execute']);

function hiddenGraphQLName(span: ReadableSpan): string {
  const field = span.attributes['graphql.field.name'];
  // GraphQLResolveInfo.fieldName is schema-derived; the upstream span name uses the alias-bearing path.
  if (typeof field === 'string' && /^[_A-Za-z][_0-9A-Za-z]*$/.test(field)) return `graphql.resolve ${field}`;
  const operation = span.attributes['graphql.operation.type'];
  if (operation === 'query' || operation === 'mutation' || operation === 'subscription') {
    return `${operation} ${REDACTED_VALUE}`;
  }
  // Unknown/future upstream span names must not become another input channel.
  return fixedGraphQLNames.has(span.name) ? span.name : REDACTED_VALUE;
}

type PrivacyPolicy = Readonly<TraceConfig & Pick<PrivacyOptions, 'captureContent'>>;
const resolvePolicy = (options: PrivacyOptions): PrivacyPolicy => Object.freeze({
  ...generateTraceConfig(options), captureContent: options.captureContent,
});

/** Filter only upstream HTTP/GraphQL spans, without mutating other exporters' view. */
export function sanitizeSpan(span: ReadableSpan, options: PrivacyOptions = {}): ReadableSpan {
  return sanitizeWithPolicy(span, resolvePolicy(options));
}

function sanitizeWithPolicy(span: ReadableSpan, policy: PrivacyPolicy): ReadableSpan {
  if (!scopes.has(span.instrumentationScope.name)) return span;
  const hideInputIdentifiers = policy.hideInputs || policy.hideInputMessages || policy.hideInputText;
  const keepSource = policy.captureContent && !hideInputIdentifiers;
  const keepErrors = policy.captureContent && !policy.hideErrorDetails;
  const text = (value: string): string => policy.piiRedaction ? redactPiiInString(value) : value;
  const redact = (value: Attributes[string]): Attributes[string] => {
    if (typeof value === 'string') return text(value);
    if (Array.isArray(value)) return value.map(item => typeof item === 'string' ? text(item) : item) as typeof value;
    return value;
  };
  const attributes: Attributes = {};
  for (const [key, value] of Object.entries(span.attributes)) {
    if (metadata.has(key) || (keepSource && key === 'graphql.source')) {
      attributes[key] = hideInputIdentifiers && inputIdentifiers.has(key) ? REDACTED_VALUE : redact(value);
    }
  }
  // Unknown event names and link attributes can themselves contain payloads.
  const events = span.events.filter(event => event.name === 'exception').map(event => {
    const attributes: Attributes = {};
    for (const key of keepErrors ? ['exception.type', 'exception.message'] : ['exception.type']) {
      if (event.attributes?.[key] !== undefined) attributes[key] = redact(event.attributes[key]);
    }
    return { ...event, attributes };
  });
  const status = { code: span.status.code, ...(keepErrors && span.status.message !== undefined
    ? { message: text(span.status.message) } : {}) };
  const name = hideInputIdentifiers && span.instrumentationScope.name === '@opentelemetry/instrumentation-graphql'
    ? hiddenGraphQLName(span) : span.name;
  return Object.create(span, {
    name: { value: typeof name === 'string' ? text(name) : name, enumerable: true },
    attributes: { value: attributes, enumerable: true },
    events: { value: events, enumerable: true },
    links: { value: span.links.map(link => ({ ...link, attributes: {} })), enumerable: true },
    status: { value: status, enumerable: true },
  });
}

/** Destination-local policy; application-created spans are intentionally unchanged. */
export class FiSanitizingExporter implements SpanExporter {
  private readonly policy: PrivacyPolicy;
  private readonly pending = new Set<Promise<void>>();
  private failed = false;
  private shutdownPromise?: Promise<void>;
  constructor(private readonly inner: SpanExporter, options: PrivacyOptions = {}) {
    this.policy = resolvePolicy(options);
  }
  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    let sanitized: ReadableSpan[];
    try {
      sanitized = spans.map(span => sanitizeWithPolicy(span, this.policy));
    } catch {
      this.failed = true;
      // Never forward the originals, or include the failed payload in diagnostics.
      callback({ code: ExportResultCode.FAILED, error: new Error('Apollo trace sanitization failed') });
      return;
    }
    let finish!: () => void;
    const done = new Promise<void>(resolve => { finish = resolve; });
    this.pending.add(done);
    try {
      this.inner.export(sanitized, result => {
        if (result.code !== ExportResultCode.SUCCESS) this.failed = true;
        this.pending.delete(done);
        finish();
        callback(result.code === ExportResultCode.SUCCESS ? result : {
          code: ExportResultCode.FAILED, error: new Error('Apollo telemetry export failed'),
        });
      });
    } catch {
      this.failed = true;
      this.pending.delete(done);
      finish();
      callback({ code: ExportResultCode.FAILED, error: new Error('Apollo telemetry export failed') });
    }
  }
  private async waitForPending(): Promise<void> {
    if (this.pending.size === 0) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        Promise.all([...this.pending]),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error('Apollo telemetry export timed out')), 4000);
        }),
      ]);
    } finally { if (timer) clearTimeout(timer); }
  }
  async forceFlush(): Promise<void> {
    await this.waitForPending();
    await this.inner.forceFlush?.();
    if (this.failed) {
      this.failed = false;
      throw new Error('Apollo telemetry export failed');
    }
  }
  shutdown(): Promise<void> {
    return this.shutdownPromise ??= (async () => {
      try {
        await this.waitForPending();
        if (this.failed) throw new Error('Apollo telemetry export failed');
      } finally { await this.inner.shutdown(); }
    })();
  }
}
