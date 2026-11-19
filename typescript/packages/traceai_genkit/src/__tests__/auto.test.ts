import { createContextKey, createTraceState, ROOT_CONTEXT, trace, type Span } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { markSecretDiscoveryFailed, registerSecret, setSession } from "@traceai/fi-core";
import { privacyFilter } from "../../../traceai_auto/src/export-privacy";
import { shouldSkip, trackCoverage } from "../../../traceai_auto/src/wire/fetch";
import { enable, type GenkitAutoSpanProcessor } from "../index";
import { flowAttributes, generateAttributes, modelAttributes, v1Span } from "./fixtures";

const AGENT_KEY = Symbol.for("traceai.auto.agent.v1");
const globals = globalThis as unknown as Record<symbol, unknown>;
const previousAgent = globals[AGENT_KEY];
const savedDisabled = process.env.OTEL_SDK_DISABLED;

function sink() {
  return { onStart: jest.fn(), onEnd: jest.fn() };
}

function installAgent(target: GenkitAutoSpanProcessor, traceConfig = {}) {
  globals[AGENT_KEY] = { processor: target, config: { apiKey: "test-api-key", secretKey: "test-secret-key", traceConfig } };
}

function model() {
  return v1Span({ name: "fixture/mock", attributes: modelAttributes(), spanId: "1111111111111111" });
}

beforeEach(() => {
  delete globals[AGENT_KEY];
  delete process.env.OTEL_SDK_DISABLED;
});
afterAll(() => {
  if (previousAgent === undefined) delete globals[AGENT_KEY];
  else globals[AGENT_KEY] = previousAgent;
  if (savedDisabled === undefined) delete process.env.OTEL_SDK_DISABLED;
  else process.env.OTEL_SDK_DISABLED = savedDisabled;
});

describe("Genkit zero-argument enable", () => {
  it("uses only the agent-owned export processor, keeping provider and signal handlers unchanged", () => {
    const target = sink();
    const end = target.onEnd;
    installAgent(target);
    const provider = trace.getTracerProvider();
    const handlers = [process.rawListeners("SIGTERM"), process.rawListeners("SIGINT"), process.rawListeners("beforeExit")];
    expect(enable()).toEqual({ status: "instrumented" });
    expect(trace.getTracerProvider()).toBe(provider);
    expect([process.rawListeners("SIGTERM"), process.rawListeners("SIGINT"), process.rawListeners("beforeExit")]).toEqual(handlers);
    const native = model();
    target.onStart(native, ROOT_CONTEXT);
    target.onEnd(native);
    const [copy] = end.mock.calls[0];
    expect(copy).not.toBe(native);
    expect(copy.attributes["gen_ai.span.kind"]).toBe("LLM");
    expect(copy.resource).toBe(native.resource);
    expect(copy.spanContext()).toEqual(native.spanContext());
    expect(native.attributes["gen_ai.span.kind"]).toBeUndefined();
  });

  it("is idempotent and keeps the first configuration, including across module reloads", () => {
    const target = sink();
    const end = target.onEnd;
    installAgent(target, { hideInputs: true });
    const first = enable();
    const wrapped = target.onEnd;
    expect(enable({ traceConfig: { hideInputs: false } })).toBe(first);
    jest.isolateModules(() => {
      const another = require("../auto") as typeof import("../auto");
      expect(another.enable()).toBe(first);
    });
    expect(target.onEnd).toBe(wrapped);
    process.env.OTEL_SDK_DISABLED = "true";
    expect(enable()).toBe(first); // Disable-after-enable is deliberately unsupported.
    const native = model();
    target.onStart(native, ROOT_CONTEXT);
    target.onEnd(native);
    expect(end).toHaveBeenCalledTimes(1);
    expect(end.mock.calls[0][0].attributes["input.value"]).toBe("__REDACTED__");
  });

  it("preserves non-Genkit spans by identity", () => {
    const target = sink();
    const end = target.onEnd;
    enable({ spanProcessor: target });
    const native = { ...model(), attributes: {}, instrumentationLibrary: { name: "application" } };
    target.onEnd(native);
    expect(end).toHaveBeenCalledWith(native);
    expect(end.mock.calls[0][0]).toBe(native);
  });

  it("maps session metadata on the copy and preserves the target processor's original parent context", () => {
    const requestKey = createContextKey("test-request-credentials");
    const secrets = new WeakMap<object, unknown>();
    const end = jest.fn((span) => {
      expect(secrets.get(span)).toEqual(["provider-secret"]);
      expect(span.attributes["session.id"]).toBe("zero-code-session");
    });
    const target = {
      onStart(span: object, parent: typeof ROOT_CONTEXT) { secrets.set(span, parent.getValue(requestKey)); },
      onEnd: end,
    };
    enable({ spanProcessor: target });
    const parent = setSession(ROOT_CONTEXT, { sessionId: "zero-code-session" }).setValue(requestKey, ["provider-secret"]);
    const native = model();
    target.onStart(native, parent);
    target.onEnd(native);
    expect(end).toHaveBeenCalledTimes(1);
    expect(native.attributes["session.id"]).toBeUndefined();
  });

  it("maps before the existing export-time privacy boundary, not instead of it", () => {
    const end = jest.fn((span) => {
      expect(span.attributes["gen_ai.request.model"]).toBe("mock");
      // A stand-in final boundary confirms the original onEnd still owns export.
      return "existing-exporter";
    });
    const target: GenkitAutoSpanProcessor = { onStart() {}, onEnd: end };
    enable({ spanProcessor: target });
    const native = model();
    target.onStart(native, ROOT_CONTEXT);
    target.onEnd(native);
    expect(end).toHaveBeenCalledTimes(1);
  });

  it("marks native model coverage before HTTP, across intermediate spans, without changing app context or attributes", () => {
    const branch: GenkitAutoSpanProcessor = { onStart: trackCoverage, onEnd() {} };
    enable({ spanProcessor: branch });
    const native = model();
    const before = { ...native.attributes };
    const active = trace.setSpan(ROOT_CONTEXT, native as unknown as Span);
    expect(shouldSkip(active)).toBe(false);
    branch.onStart(native, ROOT_CONTEXT);
    expect(shouldSkip(active)).toBe(true);
    const intermediate = v1Span({ name: "native-sdk-client", spanId: "3333333333333333", attributes: {} });
    branch.onStart(intermediate, active);
    expect(shouldSkip(trace.setSpan(ROOT_CONTEXT, intermediate as unknown as Span))).toBe(true);
    const chain = v1Span({ name: "flow", spanId: "4444444444444444", attributes: { "genkit:type": "action", "genkit:metadata:subtype": "flow" } });
    branch.onStart(chain, ROOT_CONTEXT);
    expect(shouldSkip(trace.setSpan(ROOT_CONTEXT, chain as unknown as Span))).toBe(false);
    expect(native.attributes).toEqual(before);
    expect(trace.getSpan(active)).toBe(native);
    expect(trace.getSpan(ROOT_CONTEXT)).toBeUndefined();
  });

  it("survives the real agent sanitizer with identity, timing, hide flags and request credentials intact", () => {
    const credentialKey = createContextKey("test-boundary-credentials");
    const secrets = new WeakMap<object, string[]>();
    const privacy = privacyFilter({ hideInputText: true, hideInputImages: true }, ["fi-api-secret"]);
    const exported: ReadableSpan[] = [];
    const branch: GenkitAutoSpanProcessor = {
      onStart(span, parent) { secrets.set(span, parent.getValue(credentialKey) as string[]); },
      onEnd(span) { exported.push(privacy.span(span as ReadableSpan, secrets.get(span))); },
    };
    enable({ spanProcessor: branch });
    const native = v1Span({ name: "generate", spanId: "2222222222222222", attributes: {
      ...generateAttributes(),
      "genkit:output": JSON.stringify({ message: { role: "model", content: [{ text: "answer" }] }, request: { messages: ["INPUT_SHOULD_HIDE"] } }),
      "credential-echo": "provider-secret and fi-api-secret",
    } });
    branch.onStart(native, ROOT_CONTEXT.setValue(credentialKey, ["provider-secret"]));
    branch.onEnd(native);
    const [copy] = exported;
    expect(copy.spanContext()).toEqual(native.spanContext());
    expect(copy.startTime).toEqual(native.startTime);
    expect(copy.endTime).toEqual(native.endTime);
    expect(copy.duration).toEqual(native.duration);
    // Opaque input aliases use the canonical fi-core hidden-content marker.
    expect(copy.attributes["input.value"]).toBe("__REDACTED__");
    expect(copy.attributes["output.value"]).toContain("answer");
    const encoded = JSON.stringify(copy);
    for (const text of ["INPUT_SHOULD_HIDE", "provider-secret", "fi-api-secret", "genkit:input", "genkit:output"]) expect(encoded).not.toContain(text);
    expect(native.attributes["genkit:output"]).toContain("INPUT_SHOULD_HIDE");
  });

  it("uses the process registry on every mapped surface without credential-context inheritance", () => {
    const token = "registry-native-evidence-12345";
    registerSecret(token); // A plugin credential observed outside a transport.
    const privacy = privacyFilter({ hideInputs: true, hideErrorDetails: false });
    const exported: ReadableSpan[] = [];
    const branch: GenkitAutoSpanProcessor = {
      onStart() {},
      onEnd(span) { exported.push(privacy.span(span as ReadableSpan)); },
    };
    enable({ spanProcessor: branch, traceConfig: { hideInputs: true } });
    const native = v1Span({ name: `flow ${token}`, spanId: "6666666666666666", attributes: {
      ...flowAttributes(), "genkit:input": JSON.stringify({ apiKey: token }),
      "genkit:output": JSON.stringify(`echo ${token}`),
    }, status: { code: 2, message: `failure ${token}` }, events: [{
      time: [1, 0], name: `event ${token}`, attributes: { "exception.message": `failure ${token}` },
    }] });
    branch.onStart(native, ROOT_CONTEXT);
    branch.onEnd(native);
    expect(exported).toHaveLength(1);
    expect(exported[0].attributes["output.value"]).toContain("echo <redacted>");
    expect(JSON.stringify(exported)).not.toContain(token);
    expect(native.attributes["genkit:input"]).toContain(token);
  });

  it("maps detached pre-activation spans without requiring request ancestry", () => {
    const target = sink();
    const end = target.onEnd;
    enable({ spanProcessor: target });
    const native = model();
    target.onEnd(native);
    expect(end).toHaveBeenCalledTimes(1);
    const [copy] = end.mock.calls[0];
    expect(copy.spanContext()).toEqual(native.spanContext());
    expect(copy.startTime).toEqual(native.startTime);
    expect(copy.endTime).toEqual(native.endTime);
    expect(copy.duration).toEqual(native.duration);
    expect(copy.attributes["gen_ai.span.kind"]).toBe("LLM");
    expect(copy.attributes["output.value"]).toBeDefined();
  });

  it.each(["forceFlush", "shutdown"] as const)("leaves %s unchanged and exports children before the root ends", async method => {
    const lifecycle = jest.fn(async () => {});
    const target = { ...sink(), forceFlush: lifecycle, shutdown: lifecycle };
    const end = target.onEnd;
    expect(enable({ spanProcessor: target }).status).toBe("instrumented");
    expect(target[method]).toBe(lifecycle);
    const root = v1Span({ name: "root", spanId: "7777777777777777", attributes: flowAttributes() });
    target.onStart(root, ROOT_CONTEXT);
    const child = model();
    target.onStart(child, trace.setSpan(ROOT_CONTEXT, root as unknown as Span));
    target.onEnd(child);
    expect(end).toHaveBeenCalledTimes(1);
    expect(end.mock.calls[0][0].attributes["output.value"]).toBeDefined();
    await target[method]();
    expect(lifecycle).toHaveBeenCalledTimes(1);
    target.onEnd(root);
    expect(end).toHaveBeenCalledTimes(2);
  });

  it("exports large sets of completed children immediately with every span ID intact", () => {
    const target = sink();
    const end = target.onEnd;
    enable({ spanProcessor: target });
    const root = v1Span({ name: "root", spanId: "7777777777777777", attributes: flowAttributes() });
    target.onStart(root, ROOT_CONTEXT);
    for (let i = 0; i < 260; i++) {
      const child = v1Span({ name: "child", spanId: (i + 100).toString(16).padStart(16, "0"), attributes: modelAttributes() });
      target.onStart(child, trace.setSpan(ROOT_CONTEXT, root as unknown as Span));
      target.onEnd(child);
      expect(end).toHaveBeenCalledTimes(i + 1);
    }
    target.onEnd(root);
    expect(end).toHaveBeenCalledTimes(261);
    expect(new Set(end.mock.calls.map(([span]) => span.spanContext().spanId)).size).toBe(261);
  });

  it("uses newly registered credentials for background descendants and unrelated spans", () => {
    const rootToken = "root-background-credential-12345";
    const childToken = "child-background-credential-12345";
    const privacy = privacyFilter({ hideInputs: true });
    const exported: ReadableSpan[] = [];
    const target: GenkitAutoSpanProcessor = {
      onStart() {},
      onEnd(span) { exported.push(privacy.span(span as ReadableSpan)); },
    };
    enable({ spanProcessor: target, traceConfig: { hideInputs: true } });
    registerSecret(rootToken);
    const root = v1Span({ name: "root", spanId: "7777777777777777", attributes: flowAttributes() });
    target.onStart(root, ROOT_CONTEXT);
    target.onEnd(root);
    const child = model();
    child.attributes["genkit:output"] = JSON.stringify(`echo ${rootToken} and ${childToken}`);
    target.onStart(child, trace.setSpan(ROOT_CONTEXT, root as unknown as Span));
    registerSecret(childToken);
    target.onEnd(child);
    expect(exported[1].attributes["output.value"]).toContain("echo <redacted> and <redacted>");
    const unrelated = { ...child, instrumentationLibrary: { name: "application" }, attributes: { "output.value": childToken } };
    target.onEnd(unrelated);
    for (const token of [rootToken, childToken]) expect(JSON.stringify(exported)).not.toContain(token);
  });

  it("leaves registry failure to the core fail-closed boundary without losing identity or timing", () => {
    const key = Symbol.for("traceai.secrets");
    const root = globalThis as unknown as Record<symbol, unknown>;
    const saved = Object.getOwnPropertyDescriptor(root, key);
    delete root[key];
    try {
      markSecretDiscoveryFailed();
      const privacy = privacyFilter();
      const exported: ReadableSpan[] = [];
      const target: GenkitAutoSpanProcessor = { onStart() {}, onEnd(span) { exported.push(privacy.span(span as ReadableSpan)); } };
      enable({ spanProcessor: target });
      const native = model();
      target.onStart(native, ROOT_CONTEXT);
      target.onEnd(native);
      expect(exported).toHaveLength(1);
      expect(exported[0].attributes).toEqual({});
      // Core's minimal refusal view uses this constant even without SDK loss.
      // The empty attributes above also prove it did not invent a host-loss flag.
      expect(exported[0].name).toBe("<unknown: host-truncated>");
      expect(exported[0].events).toEqual([]);
      expect(exported[0].links).toEqual([]);
      expect(exported[0].resource.attributes).toEqual({});
      expect(exported[0].instrumentationScope).toEqual({ name: "<unknown: host-truncated>" });
      expect(exported[0].status).toEqual({ code: native.status.code });
      expect(exported[0].spanContext()).toEqual(native.spanContext());
      expect(exported[0].startTime).toEqual(native.startTime);
      expect(exported[0].endTime).toEqual(native.endTime);
      expect(exported[0].duration).toEqual(native.duration);
    } finally {
      if (saved) Object.defineProperty(root, key, saved);
      else delete root[key];
    }
  });

  it("scrubs longest-first registry values in arrays, links, resource, scope and tracestate without mutating native telemetry", () => {
    const short = "overlapping-registry-12345";
    const long = short + "-longer";
    registerSecret(short);
    registerSecret(long);
    const privacy = privacyFilter();
    const exported: ReadableSpan[] = [];
    const target: GenkitAutoSpanProcessor = { onStart() {}, onEnd(span) { exported.push(privacy.span(span as ReadableSpan)); } };
    enable({ spanProcessor: target });
    const base = model();
    const ctx = { ...base.spanContext(), traceState: createTraceState("vendor=" + long) };
    const native = {
      ...base, spanContext: () => ctx,
      attributes: { ...base.attributes, "custom.values": [long, short] },
      resource: { attributes: { "custom.value": long } },
      instrumentationLibrary: { name: "genkit-tracer", version: long, attributes: { "custom.value": short } },
      links: [{ context: ctx, attributes: { "custom.value": long } }],
    };
    target.onStart(native, ROOT_CONTEXT);
    target.onEnd(native);
    expect(exported).toHaveLength(1);
    expect(exported[0].attributes["custom.values"]).toEqual(["<redacted>", "<redacted>"]);
    expect(exported[0].spanContext().traceState?.get("vendor")).toBe("<redacted>");
    expect(exported[0].links[0].attributes!["custom.value"]).toBe("<redacted>");
    expect(exported[0].resource.attributes["custom.value"]).toBe("<redacted>");
    expect(exported[0].instrumentationScope.version).toBe("<redacted>");
    for (const secret of [long, short]) expect(JSON.stringify(exported)).not.toContain(secret);
    expect(native.attributes["custom.values"]).toEqual([long, short]);
    expect(native.resource.attributes["custom.value"]).toBe(long);
    expect(native.spanContext().traceState.get("vendor")).toBe(long);
  });

  it.each([NaN, Infinity, -Infinity])("keeps mapped spans with non-finite attributes through the core boundary (%s)", value => {
    const privacy = privacyFilter();
    const exported: ReadableSpan[] = [];
    const target: GenkitAutoSpanProcessor = { onStart() {}, onEnd(span) { exported.push(privacy.span(span as ReadableSpan)); } };
    enable({ spanProcessor: target });
    const native = model();
    native.attributes["custom.number"] = value;
    target.onStart(native, ROOT_CONTEXT);
    target.onEnd(native);
    expect(exported).toHaveLength(1);
    expect(exported[0].attributes["custom.number"]).toBe(String(value));
    expect(exported[0].spanContext()).toEqual(native.spanContext());
    expect(exported[0].duration).toEqual(native.duration);
  });

  it("returns manual without an initialized agent, and can enable additively later", () => {
    expect(enable()).toEqual({ status: "manual", reason: expect.stringContaining("initialized @traceai/auto") });
    installAgent(sink());
    expect(enable().status).toBe("instrumented");
  });

  it("respects SDK-disabled and missing-credential states without installing anything", () => {
    const target = sink();
    const end = target.onEnd;
    installAgent(target);
    process.env.OTEL_SDK_DISABLED = "TRUE";
    expect(enable().status).toBe("disabled");
    delete process.env.OTEL_SDK_DISABLED;
    globals[AGENT_KEY] = { processor: target, config: { sdkDisabled: true } };
    expect(enable().status).toBe("disabled");
    globals[AGENT_KEY] = { processor: target, config: {} };
    expect(enable().status).toBe("disabled");
    expect(target.onEnd).toBe(end);
  });

  it("fails open on unsupported/frozen processors without leaving a partial wrapper", () => {
    const target = sink();
    const start = target.onStart;
    const end = target.onEnd;
    Object.defineProperty(target, "onEnd", { value: end, configurable: false, writable: false });
    expect(enable({ spanProcessor: target }).status).toBe("failed");
    expect(target.onStart).toBe(start);
    expect(target.onEnd).toBe(end);
    expect(enable({ spanProcessor: Object.freeze(sink()) }).status).toBe("failed");
    expect(enable({ spanProcessor: {} as GenkitAutoSpanProcessor }).status).toBe("failed");
  });

  it("does not leak a raw span when mapping fails or propagate exporter exceptions", () => {
    const target = sink();
    const end = target.onEnd;
    enable({ spanProcessor: target });
    const native = model();
    target.onStart(native, ROOT_CONTEXT);
    Object.defineProperty(native, "attributes", { get() { throw new Error("private text"); } });
    expect(() => target.onEnd(native)).not.toThrow();
    expect(end).not.toHaveBeenCalled();
    const broken = { onStart() { throw new Error("private key"); }, onEnd() { throw new Error("private key"); } };
    enable({ spanProcessor: broken });
    expect(() => broken.onStart()).not.toThrow();
    expect(() => broken.onEnd()).not.toThrow();
  });
});
