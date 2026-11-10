import { afterAll, beforeAll, describe, expect, it, jest } from "@jest/globals";
import {
  context,
  ROOT_CONTEXT,
  SpanKind,
  trace,
  type Context,
  type SpanOptions,
  type Tracer,
} from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { isSessionIdentity } from "../privacy-runtime";
import { getAttributesFromContext, setAttributes, setSession, setUser } from "../trace/contextAttributes";
import { REDACTED_VALUE } from "../trace/trace-config/constants";
import { FITracer } from "../trace/trace-config/FITracer";
import type { TraceConfigOptions } from "../trace/trace-config/types";

const contextManager = new AsyncLocalStorageContextManager();
beforeAll(() => {
  context.setGlobalContextManager(contextManager.enable());
});
afterAll(() => {
  context.disable();
  contextManager.disable();
});

function identityContext(base: Context = ROOT_CONTEXT): Context {
  return setUser(setSession(base, { sessionId: "session-demo" }), { userId: "user-demo" });
}

// Keep the native tracer offline and observable at creation time. The real FI
// context setters, attribute reader, privacy admission and FISpan remain in use.
describe.each(["standalone", "auto"] as const)("T6-TS-CORE-AUTO-002 startSpan context (%s)", (mode) => {
  function fixture(traceConfig: TraceConfigOptions = {}) {
    const nativeSpan = trace.wrapSpanContext({
      traceId: "1".repeat(32),
      spanId: "2".repeat(16),
      traceFlags: 1,
    });
    const startSpan = jest.fn<Tracer["startSpan"]>(() => nativeSpan);
    const nativeTracer = {
      startSpan,
      startActiveSpan(): never {
        throw new Error("FITracer should create spans through native startSpan");
      },
      getSpanLimits: () => ({ attributeCountLimit: 128 }),
    };
    const tracer = new FITracer({
      tracer: mode === "auto"
        ? FITracer.autoProvider({ getTracer: () => nativeTracer }).getTracer("context-regression")
        : nativeTracer,
      traceConfig: { piiRedaction: false, ...traceConfig },
    });
    return { tracer, nativeSpan, startSpan };
  }

  it.each(["implicit", "explicit"] as const)("inherits typed identities and forwards the %s context", (source) => {
    const { tracer, startSpan } = fixture();
    const parent = trace.wrapSpanContext({
      traceId: "3".repeat(32),
      spanId: "4".repeat(16),
      traceFlags: 1,
    });
    const ctx = identityContext(trace.setSpan(ROOT_CONTEXT, parent));
    const otherContext = setUser(setSession(ROOT_CONTEXT, { sessionId: "session-other" }), { userId: "user-other" });
    const ambient = source === "implicit" ? ctx : otherContext;
    const span = context.with(ambient, () => {
      const child = source === "implicit"
        ? tracer.startSpan("child")
        : tracer.startSpan("child", undefined, ctx);
      // startSpan must not install the child as the active span.
      expect(context.active()).toBe(ambient);
      return child;
    });

    expect(startSpan).toHaveBeenCalledTimes(1);
    expect(startSpan).toHaveBeenCalledWith("child", {
      attributes: { "session.id": "session-demo", "user.id": "user-demo" },
    }, ctx);
    expect(trace.getSpan(startSpan.mock.calls[0][2]!)).toBe(parent);
    expect(isSessionIdentity(span.spanContext(), "session-demo")).toBe(true);
    span.end();
  });

  it("keeps startActiveSpan as the typed-context control", () => {
    const { tracer, startSpan } = fixture();
    const ctx = identityContext();
    tracer.startActiveSpan("control", {}, ctx, (span) => {
      expect(isSessionIdentity(span.spanContext(), "session-demo")).toBe(true);
      span.end();
    });
    expect(startSpan).toHaveBeenCalledWith("control", {
      attributes: { "session.id": "session-demo", "user.id": "user-demo" },
    }, ctx);
  });

  it("lets caller attributes win without binding an overridden session or mutating inputs", () => {
    const { tracer, startSpan } = fixture();
    const ctx = setAttributes(identityContext(), { "context.only": true, label: "context label" });
    const attributes = Object.freeze({
      "session.id": "session-override",
      "user.id": "user-override",
      label: "caller label",
    });
    const options: SpanOptions = Object.freeze({
      attributes,
      kind: SpanKind.CLIENT,
      startTime: 1700000000000,
      root: true,
    });
    const before = getAttributesFromContext(ctx);
    const span = tracer.startSpan("override", options, ctx);

    expect(startSpan).toHaveBeenCalledWith("override", {
      ...options,
      attributes: { "context.only": true, ...attributes },
    }, ctx);
    expect(getAttributesFromContext(ctx)).toEqual(before);
    expect(options.attributes).toBe(attributes);
    expect(isSessionIdentity(span.spanContext(), "session-demo")).toBe(false);
    expect(isSessionIdentity(span.spanContext(), "session-override")).toBe(false);
    span.end();
  });

  it("honors explicit undefined identity overrides", () => {
    const { tracer, startSpan } = fixture();
    const ctx = identityContext();
    const span = tracer.startSpan("omit", {
      attributes: { "session.id": undefined, "user.id": undefined },
    }, ctx);
    expect(startSpan).toHaveBeenCalledWith("omit", { attributes: {} }, ctx);
    expect(isSessionIdentity(span.spanContext(), "session-demo")).toBe(false);
    span.end();
  });

  it("does not inherit ambient identities when ROOT_CONTEXT is explicit", () => {
    const { tracer, startSpan } = fixture();
    context.with(identityContext(), () => {
      const span = tracer.startSpan("root", { attributes: { label: "root span" } }, ROOT_CONTEXT);
      expect(isSessionIdentity(span.spanContext(), "session-demo")).toBe(false);
      span.end();
    });
    expect(startSpan).toHaveBeenCalledWith("root", {
      attributes: { label: "root span" },
    }, ROOT_CONTEXT);
  });

  it("applies privacy before native creation and does not bind a changed session", () => {
    const { tracer, startSpan } = fixture({ hideInputs: true, piiRedaction: true });
    const ctx = setAttributes(
      setUser(setSession(ROOT_CONTEXT, { sessionId: "person@example.com" }), { userId: "person@example.com" }),
      { "input.value": "private fixture input" },
    );
    const span = tracer.startSpan("private", {}, ctx);
    expect(startSpan).toHaveBeenCalledWith("private", {
      attributes: {
        "session.id": "<EMAIL_ADDRESS>",
        "user.id": "<EMAIL_ADDRESS>",
        "input.value": REDACTED_VALUE,
      },
    }, ctx);
    expect(isSessionIdentity(span.spanContext(), "person@example.com")).toBe(false);
    expect(isSessionIdentity(span.spanContext(), "<EMAIL_ADDRESS>")).toBe(false);
    span.end();
  });
});
