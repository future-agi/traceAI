import type { Span } from '@opentelemetry/api';
import { types } from 'node:util';

// Optional SDK interop, including mixed ESM/CJS. The wrapper exports only
// admitted identity and write eligibility; it never exposes its native span or a
// callback that can operate on it. All attribute writes still use FISpan.
export const AGENT_VIEW = Symbol.for('futureagi.runtime.agent-identity.v1');
export const AGENT_NAME = 'gen_ai.agent.name';
export const AGENT_ID = 'gen_ai.agent.id';
const WRITER = Symbol.for('futureagi.runtime.context-agent.writer.v1');
export const SET_CONTEXT_NAME = Symbol.for('futureagi.runtime.set-context-name.v1');
export const FILL_IDENTITY = Symbol.for('futureagi.runtime.fill-agent-identity.v1');
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

// Inspect only the real SDK's data slots, without publishing a host value or
// interpreting a missing/redacted export as a missing native attribute.
export function identitySlots(span: Span, lastContextName?: string) {
  if (types.isProxy(span)) return;
  const native = Object.getOwnPropertyDescriptor(span, '_droppedAttributesCount');
  const slot = Object.getOwnPropertyDescriptor(span, 'attributes');
  const attrs = slot?.value;
  if (typeof native?.value !== 'number' || !slot?.writable || !attrs || typeof attrs !== 'object' || types.isProxy(attrs)) return;
  const proto = Object.getPrototypeOf(attrs);
  if (proto !== null && proto !== Object.prototype) return;
  return { nameEmpty: !(AGENT_NAME in attrs), idEmpty: !(AGENT_ID in attrs),
    matchesLast: lastContextName !== undefined && Object.getOwnPropertyDescriptor(attrs, AGENT_NAME)?.value === lastContextName };
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
