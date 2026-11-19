import type { Attributes, Span } from '@opentelemetry/api';

// Optional SDK interop, including mixed ESM/CJS. The wrapper exports only
// admitted identity and a writer bit; it never exposes its native span or a
// callback that can operate on it. All attribute writes still use FISpan.
export const AGENT_VIEW = Symbol.for('futureagi.runtime.agent-identity.v1');
export const AGENT_NAME = 'gen_ai.agent.name';
const WRITER = Symbol.for('futureagi.runtime.context-agent.writer.v1');
type Origin = { automatic: boolean };
type Setter = Span['setAttribute'] & { [WRITER]?: Origin };

export function writer(span: Span): Origin | undefined {
  return (span.setAttribute as Setter)[WRITER];
}

function markNative(span: Span): void {
  const known = writer(span);
  if (known) { known.automatic = true; return; }
  // This can be the first public read on an existing standalone FISpan;
  // there need not have been an SDK name stamp at native onStart.
  const single = span.setAttribute, bulk = span.setAttributes;
  const origin = { automatic: true };
  const observed: Setter = function(this: Span, key, value) {
    if (key === AGENT_NAME) origin.automatic = false;
    return single.call(this, key, value);
  };
  observed[WRITER] = origin;
  try {
    span.setAttributes = function(attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, AGENT_NAME)) origin.automatic = false;
      return bulk.call(this, attrs);
    };
    span.setAttribute = observed;
  } catch { /* Frozen foreign writer: don't claim a context origin. */ }
}

export function agentView(span: Span, mark?: true): {name?: string; id?: string; automatic: boolean} {
  const attrs = (span as Span & {attributes?: Attributes}).attributes;
  const name = attrs?.[AGENT_NAME], id = attrs?.['gen_ai.agent.id'];
  if (mark && typeof name === 'string') markNative(span);
  return {name: typeof name === 'string' ? name : undefined, id: typeof id === 'string' ? id : undefined,
    automatic: writer(span)?.automatic === true};
}
