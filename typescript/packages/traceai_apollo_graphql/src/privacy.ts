import type { Attributes } from '@opentelemetry/api';
import { ExportResultCode, type ExportResult } from '@opentelemetry/core';
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace';

export interface PrivacyOptions {
  /** Include masked query source and error messages, but never stacks or variable values. */
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

/** Filter only upstream HTTP/GraphQL spans, without mutating other exporters' view. */
export function sanitizeSpan(span: ReadableSpan, options: PrivacyOptions = {}): ReadableSpan {
  if (!scopes.has(span.instrumentationScope.name)) return span;
  const attributes: Attributes = {};
  for (const [key, value] of Object.entries(span.attributes)) {
    if (metadata.has(key) || (options.captureContent && key === 'graphql.source')) {
      attributes[key] = value;
    }
  }
  // Unknown event names and link attributes can themselves contain payloads.
  const events = span.events.filter(event => event.name === 'exception').map(event => {
    const attributes: Attributes = {};
    for (const key of options.captureContent
      ? ['exception.type', 'exception.message'] : ['exception.type']) {
      if (event.attributes?.[key] !== undefined) attributes[key] = event.attributes[key];
    }
    return { ...event, attributes };
  });
  return Object.create(span, {
    attributes: { value: attributes, enumerable: true },
    events: { value: events, enumerable: true },
    links: { value: span.links.map(link => ({ ...link, attributes: {} })), enumerable: true },
    status: {
      value: options.captureContent ? { ...span.status } : { code: span.status.code },
      enumerable: true,
    },
  });
}

/** Destination-local policy; application-created spans are intentionally unchanged. */
export class FiSanitizingExporter implements SpanExporter {
  private readonly options: Readonly<PrivacyOptions>;
  constructor(private readonly inner: SpanExporter, options: PrivacyOptions = {}) {
    this.options = Object.freeze({ ...options });
  }
  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    let sanitized: ReadableSpan[];
    try {
      sanitized = spans.map(span => sanitizeSpan(span, this.options));
    } catch {
      // Never forward the originals, or include the failed payload in diagnostics.
      callback({ code: ExportResultCode.FAILED, error: new Error('Apollo trace sanitization failed') });
      return;
    }
    this.inner.export(sanitized, callback);
  }
  shutdown(): Promise<void> { return this.inner.shutdown(); }
  forceFlush(): Promise<void> { return this.inner.forceFlush?.() ?? Promise.resolve(); }
}
