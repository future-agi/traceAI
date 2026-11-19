import type { Span } from '@opentelemetry/api';

// Optional SDK interop, including mixed ESM/CJS. The wrapper exports only
// admitted identity and a writer bit; it never exposes its native span or a
// callback that can operate on it. All attribute writes still use FISpan.
export const AGENT_VIEW = Symbol.for('futureagi.runtime.agent-identity.v1');
export const AGENT_NAME = 'gen_ai.agent.name';
export const AGENT_ID = 'gen_ai.agent.id';
const WRITER = Symbol.for('futureagi.runtime.context-agent.writer.v1');
export const SET_CONTEXT_NAME = Symbol.for('futureagi.runtime.set-context-name.v1');
export const WATCH_NAME = Symbol.for('traceai.fi.standalone-name-observer.v1');
type Origin = { automatic: boolean; beforeHost?: WeakSet<Span> };
type Setter = Span['setAttribute'] & { [WRITER]?: Origin };

export function writer(span: Span): Origin | undefined {
  return (span.setAttribute as Setter)[WRITER];
}

// An adapter may borrow a native method. Its writer is not evidence that we
// saw this adapter before host code. Keep only weak object identities.
export function observedWriter(span: Span): Origin | undefined {
  const origin = writer(span);
  return origin?.beforeHost?.has(span) ? origin : undefined;
}

// Called by our processor registry *before* dispatching user onStart hooks.
// A setter bound by one of those hooks now shares this exact writer bit. No
// identity string, native storage, or span reference is kept in the mark.
export function observeBeforeHost(span: Span): void {
  const known = writer(span);
  if (known) { (known.beforeHost ??= new WeakSet()).add(span); return; }
  const single = span.setAttribute, bulk = span.setAttributes;
  const origin: Origin = { automatic: false, beforeHost: new WeakSet([span]) };
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
