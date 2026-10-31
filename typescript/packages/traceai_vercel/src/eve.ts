import type { Attributes, SpanContext } from '@opentelemetry/api';
import { FISpanKind, SemanticConventions as S } from '@traceai/fi-semantic-conventions';

/** Structural across OpenTelemetry JS 1.x and 2.x; Eve owns the provider. */
export interface EveReadableSpan {
  readonly attributes: Attributes;
  readonly instrumentationScope?: { readonly name: string };
  readonly instrumentationLibrary?: { readonly name: string };
  readonly parentSpanId?: string;
  readonly parentSpanContext?: SpanContext;
  spanContext(): SpanContext;
}

export interface EveDestinationOptions<T extends EveReadableSpan> {
  /** An export processor (BatchSpanProcessor or SimpleSpanProcessor), not instrumentation. */
  processor: {
    onEnd(span: T): void;
    forceFlush(): Promise<void>;
    shutdown(): Promise<void>;
  };
  /** Opt in for this destination only, subject to Eve's native capture ceiling. */
  captureContent?: boolean;
}

export interface EveDestination<T extends EveReadableSpan> {
  readonly exportPolicy: ReadonlyArray<{
    span(): { redact: true; inputs: true; outputs: true };
    attribute(input: { key: string }): { emit: boolean };
  }>;
  readonly spanProcessors: ReadonlyArray<{
    onStart(span: T): void;
    onEnd(span: T): void;
    forceFlush(): Promise<void>;
    shutdown(): Promise<void>;
  }>;
}

const DROPPED_SCOPES = new Set(['workflow', '@vercel/otel/fetch']);
const PRINCIPAL_IDS = new Set(['agent.principal.current.id', 'agent.principal.initiator.id']);
const KINDS: Readonly<Record<string, FISpanKind | undefined>> = {
  invoke_agent: FISpanKind.AGENT,
  workflow: FISpanKind.CHAIN,
  chat: FISpanKind.LLM,
  execute_tool: FISpanKind.TOOL,
};

function scopeName(span: EveReadableSpan): string {
  return span.instrumentationScope?.name ?? span.instrumentationLibrary?.name ?? '';
}

function setContent(attributes: Attributes, source: string, target: string, mime: string): void {
  const value = attributes[source];
  if (typeof value !== 'string' || attributes[target] !== undefined) return;
  attributes[target] = value;
  try {
    JSON.parse(value);
    attributes[mime] = 'application/json';
  } catch {
    attributes[mime] = 'text/plain';
  }
}

function normalize(span: EveReadableSpan): Attributes {
  const attributes = { ...span.attributes };
  if (scopeName(span) !== 'eve.agent') return attributes;

  const operation = attributes['gen_ai.operation.name'];
  if (typeof operation === 'string' && Object.prototype.hasOwnProperty.call(KINDS, operation)) {
    attributes[S.FI_SPAN_KIND] ??= KINDS[operation];
  }
  const conversation = attributes['gen_ai.conversation.id'];
  if (typeof conversation === 'string' && conversation.length > 0) {
    attributes[S.SESSION_ID] ??= conversation;
  }

  // Keep turn/step totals inspectable without counting model usage a second time.
  if (operation === 'invoke_agent' || operation === 'workflow') {
    for (const key of Object.keys(attributes)) {
      if (key.startsWith('gen_ai.usage.')) {
        attributes[`eve.aggregate.usage.${key.slice('gen_ai.usage.'.length)}`] = attributes[key];
        delete attributes[key];
      }
    }
  }

  // These values have already passed Eve's per-destination export policy. Never
  // reach back to the original span or runtime context to recover removed content.
  setContent(attributes, 'gen_ai.input.messages', S.INPUT_VALUE, S.INPUT_MIME_TYPE);
  setContent(attributes, 'gen_ai.output.messages', S.OUTPUT_VALUE, S.OUTPUT_MIME_TYPE);
  if (operation === 'execute_tool') {
    attributes[S.TOOL_NAME] ??= attributes['gen_ai.tool.name'];
    attributes[S.TOOL_CALL_ID] ??= attributes['gen_ai.tool.call.id'];
    attributes[S.TOOL_PARAMETERS] ??= attributes['gen_ai.tool.call.arguments'];
    setContent(attributes, 'gen_ai.tool.call.arguments', S.INPUT_VALUE, S.INPUT_MIME_TYPE);
    setContent(attributes, 'gen_ai.tool.call.result', S.OUTPUT_VALUE, S.OUTPUT_MIME_TYPE);
  }
  return attributes;
}

/**
 * Pass the returned options to Eve's native `otelIntegration()`. This does not
 * register a provider, patch the AI SDK, or create spans. Eve applies the paired
 * export policy before this destination sees a span; other destinations and
 * local retention remain unchanged. Supported native contract: Eve 0.80.0.
 *
 * The supplied export processor receives ended spans only. Use a standard
 * BatchSpanProcessor or SimpleSpanProcessor, not a processor that needs onStart.
 */
export function createEveDestination<T extends EveReadableSpan>({
  processor,
  captureContent = false,
}: EveDestinationOptions<T>): EveDestination<T> {
  // Bounded ancestry for filtered parents, including parents that end before a
  // child. No trace payloads are retained. Active children retain their resolved
  // parent in a WeakMap even when old entries leave the ancestry cache.
  const droppedParents = new Map<string, SpanContext | undefined>();
  const parents = new WeakMap<object, SpanContext | undefined>();
  const key = (context: SpanContext) => `${context.traceId}/${context.spanId}`;
  const parentOf = (span: EveReadableSpan): SpanContext | undefined =>
    span.parentSpanContext ?? (span.parentSpanId
      ? { ...span.spanContext(), spanId: span.parentSpanId }
      : undefined);
  const resolveParent = (parent: SpanContext | undefined): SpanContext | undefined => {
    const seen = new Set<string>();
    while (parent && droppedParents.has(key(parent)) && !seen.has(key(parent))) {
      seen.add(key(parent));
      parent = droppedParents.get(key(parent));
    }
    return parent;
  };
  const rememberDropped = (span: EveReadableSpan) => {
    droppedParents.set(key(span.spanContext()), resolveParent(parentOf(span)));
    if (droppedParents.size > 4096) {
      droppedParents.delete(droppedParents.keys().next().value!);
    }
  };

  return {
    exportPolicy: captureContent ? [] : [{
      span: () => ({ redact: true as const, inputs: true, outputs: true }),
      attribute: ({ key }: { key: string }) => ({ emit: !PRINCIPAL_IDS.has(key) }),
    }],
    spanProcessors: [{
      onStart(span: T): void {
        if (DROPPED_SCOPES.has(scopeName(span))) rememberDropped(span);
        else parents.set(span, resolveParent(parentOf(span)));
      },
      onEnd(span: T): void {
        if (DROPPED_SCOPES.has(scopeName(span))) {
          rememberDropped(span);
          return;
        }
        const parent = parents.has(span) ? parents.get(span) : resolveParent(parentOf(span));
        parents.delete(span);
        // A read-only view preserves SDK getters, resource, timing and span IDs
        // without changing the shared span that other destinations receive.
        const view: T = Object.create(span, {
          attributes: { enumerable: true, value: normalize(span) },
          parentSpanId: { enumerable: true, value: parent?.spanId },
          parentSpanContext: { enumerable: true, value: parent },
        });
        processor.onEnd(view);
      },
      forceFlush: () => processor.forceFlush(),
      async shutdown(): Promise<void> {
        droppedParents.clear();
        await processor.shutdown();
      },
    }],
  };
}
