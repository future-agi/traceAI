import { ROOT_CONTEXT, trace, type Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { registerSecret } from "../secrets";
import { discoverSecrets, isCredentialName, mediaReplacement, scrubProviderResponse, scrubValue } from "../privacy-runtime";
import { DROPPED_CONTENT_COUNT, projectAttributes } from "../attribute-projection";
import { setSession } from "../trace/contextAttributes";
import { FITracer } from "../trace/trace-config/FITracer";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import { HIDE_ERROR_DETAILS_IMPLIED_BY } from "../trace/trace-config/constants";

const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });

function snapshot(attributes: Attributes): any {
  return { _spanContext: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 },
    name: "application", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "application" }, events: [], links: [] };
}

test.each(["default", "option-false", "env-false", "env-empty"])("H1 A1 image hiding retains scrubbed error details (%s)", async mode => {
  expect([...HIDE_ERROR_DETAILS_IMPLIED_BY].sort()).toEqual(["hideInputMessages", "hideInputText", "hideInputs", "hideOutputMessages", "hideOutputText", "hideOutputs"].sort());
  expect(generateTraceConfig({ hideInputImages: true }).hideErrorDetails).toBe(false);
  if (mode === "env-false") process.env.FI_HIDE_ERROR_DETAILS = "false";
  if (mode === "env-empty") process.env.FI_HIDE_ERROR_DETAILS = "";
  // PENDING Z-OVER: F2 cap-0 whole-scope replacement precedes I1 media markers.
  for (const base64ImageMaxLength of [undefined, 32000]) {
  const options = { hideInputImages: true, ...(mode === "option-false" ? { hideErrorDetails: false } : {}), ...(base64ImageMaxLength === undefined ? {} : { base64ImageMaxLength }) };
  expect(generateTraceConfig(options).hideErrorDetails).toBe(false);
  const image = "iVBORw0KGgoAAAANSUhEUg==";
  const omitted = `<omitted: ${image.length} base64 chars>`;
  const cases = [
    { label: "plain", message: "h1-image-error-echo", expected: "h1-image-error-echo" },
    { label: "bare-base64", message: `h1-image-error-echo ${image} end`, expected: `h1-image-error-echo ${omitted} end` },
    { label: "data-url", message: `h1-image-error-echo data:image/png;base64,${image} end`, expected: `h1-image-error-echo ${omitted} end` },
  ];
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("h1-image-errors"), traceConfig: options });
    for (const { label, message, expected } of cases) {
      const span = tracer.startSpan(label);
      span.setAttribute("error.message", message);
      span.recordException(new Error(message));
      span.setStatus({ code: 2, message });
      span.end();
      const raw = exporter.getFinishedSpans().at(-1)!;
      const unwrapped = snapshot({ "error.message": message });
      unwrapped.status = { code: 2, message };
      unwrapped.events = [{ name: "exception", time: [1, 0], attributes: {
        "exception.type": "Error", "exception.message": message,
        "exception.stacktrace": `Error: ${message}\n    at image-control.js:1:1`,
      } }];
      for (const exported of [raw, privacyFilter(options).span(raw), privacyFilter(options).span(unwrapped)]) {
        // A1 retains error details, not just a status code or an empty snapshot.
        if (base64ImageMaxLength === undefined && label === "data-url") {
          expect(exported.events[0].attributes?.["exception.message"]).toBe("[image-redacted]");
          expect(exported.status.message).toBe("[image-redacted]");
          expect(exported.attributes["error.message"]).toBe("[image-redacted]");
        } else {
        expect(exported.events[0].attributes?.["exception.message"]).toBe(expected);
        expect(exported.status.message).toBe(expected);
        expect(exported.attributes["error.message"]).toBe(expected);
        }
        expect(exported.events[0].attributes?.["exception.type"]).toBe("Error");
        expect(exported.status.code).toBe(2);
        const serialized = JSON.stringify({ attributes: exported.attributes, status: exported.status, events: exported.events });
        expect(serialized).not.toContain(image);
        expect(serialized).not.toContain("data:");
      }
    }
  } finally { await provider.shutdown(); }
  }
});

test.each([65536, 131072])("H1 performance fast paths preserve long prose and still mask late credentials (repeats=%i)", async repeats => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const prose = "Note: m\n\n".repeat(repeats);
    const secret = "synthetic-h1-late-password";
    const span = new FITracer({ tracer: provider.getTracer("h1-performance") }).startSpan("large");
    span.recordException(new Error(`${prose}password=${secret}\nend of message`));
    span.end();
    const event = exporter.getFinishedSpans()[0].events[0];
    // PENDING I1xF2-PRECEDENCE: F2 scan bound precedes I1 long-prose retention.
    if (repeats === 65536) {
      expect(String(event.attributes?.["exception.message"])).toBe(`${prose}password="<redacted>"\nend of message`);
    } else {
      expect(String(event.attributes?.["exception.message"])).toBe("[image-redacted]");
    }
    expect(JSON.stringify(event)).not.toContain(secret);
    expect(event.attributes?.["exception.type"]).toBe("Error");
  } finally { await provider.shutdown(); }
});

test.each(["ordinary", "credential-shaped"])("H1 typed FI session retains only safe bound bytes on detached reexport (%s)", async kind => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const id = kind === "ordinary" ? "h1-typed-correlation" : "sk-" + "R".repeat(24);
    const expected = kind === "ordinary" ? id : "<redacted>";
    const tracer = new FITracer({ tracer: provider.getTracer("h1-session") });
    tracer.startActiveSpan("typed", { attributes: { "control.label": "h1-public-control", "session.cookie": "h1-typed-cookie-credential" } }, setSession(ROOT_CONTEXT, { sessionId: id }), span => span.end());
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    const first = privacyFilter().span(native);
    // RED on r11: its matcher returns false even for this typed handoff.
    expect(first.attributes["session.id"]).toBe(expected);
    const second = privacyFilter().span(first);
    expect(second.attributes["session.id"]).toBe(expected);
    expect(first.spanContext()).not.toBe(native.spanContext());
    expect(second.spanContext()).not.toBe(first.spanContext());
    for (const clean of [first, second]) {
      expect(clean.attributes["control.label"]).toBe("h1-public-control");
      expect(clean.attributes["session.cookie"]).toBe("<redacted>");
    }
    if (kind === "credential-shaped") expect(JSON.stringify([first, second])).not.toContain(id);
    expect(JSON.stringify(native.attributes)).toBe(before);
    const unbound = privacyFilter().span(snapshot({ "session.id": "h1-unbound-session", "session.cookie": "h1-cookie-credential" }));
    for (const clean of [unbound, privacyFilter().span(unbound)]) {
      expect(clean.attributes).toMatchObject({ "session.id": "<redacted>", "session.cookie": "<redacted>" });
    }
    const changed = { ...snapshot({ "session.id": "h1-rewritten-session" }), _spanContext: native.spanContext() };
    const changedFirst = privacyFilter().span(changed);
    for (const clean of [changedFirst, privacyFilter().span(changedFirst)]) expect(clean.attributes["session.id"]).toBe("<redacted>");
    const surfaces = snapshot({});
    surfaces._spanContext = native.spanContext();
    surfaces.resource.attributes = { "session.id": "h1-resource-session" };
    surfaces.events = [{ name: "event", attributes: { "session.id": "h1-event-session" } }];
    surfaces.links = [{ context: native.spanContext(), attributes: { "session.id": "h1-link-session" } }];
    const surfaceFirst = privacyFilter().span(surfaces);
    for (const clean of [surfaceFirst, privacyFilter().span(surfaceFirst)]) {
      expect(clean.resource.attributes["session.id"]).toBe("<redacted>");
      expect(clean.events[0].attributes?.["session.id"]).toBe("<redacted>");
      expect(clean.links[0].attributes?.["session.id"]).toBe("<redacted>");
    }
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["native", "fi-wrapper"])("H1 raw setAttribute does not establish typed session identity (%s)", async kind => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const id = `h1-raw-${kind}-correlation`;
    const nativeTracer = provider.getTracer("h1-raw-session");
    const tracer = kind === "native" ? nativeTracer : new FITracer({ tracer: nativeTracer });
    const span = tracer.startSpan("raw");
    span.setAttribute("session.id", id);
    span.setAttribute("control.label", "h1-public-control");
    span.end();
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    expect(native.attributes["session.id"]).toBe(id);
    const first = privacyFilter().span(native);
    for (const clean of [first, privacyFilter().span(first)]) {
      expect(clean.attributes["session.id"]).toBe("<redacted>");
      expect(clean.attributes["control.label"]).toBe("h1-public-control");
      expect(JSON.stringify(clean)).not.toContain(id);
    }
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["cloned-context", "different-span"])("H1 typed session requires the bound context object (%s)", async kind => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    // Unique per row: earlier unbound discovery must not make this check vacuous.
    const id = `h1-${kind}-correlation`;
    const tracer = new FITracer({ tracer: provider.getTracer("h1-context-session") });
    tracer.startActiveSpan("origin", {}, setSession(ROOT_CONTEXT, { sessionId: id }), span => span.end());
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    expect(privacyFilter().span(native).attributes["session.id"]).toBe(id);
    const other = snapshot({ "session.id": id, "control.label": "h1-public-control" });
    if (kind === "cloned-context") {
      other._spanContext = { ...native.spanContext() };
      expect(other._spanContext).toEqual(native.spanContext());
    } else {
      const target = tracer.startSpan("target");
      other._spanContext = target.spanContext();
      target.end();
    }
    expect(other._spanContext).not.toBe(native.spanContext());
    const first = privacyFilter().span(other);
    for (const clean of [first, privacyFilter().span(first)]) {
      expect(clean.attributes["session.id"]).toBe("<redacted>");
      expect(clean.attributes["control.label"]).toBe("h1-public-control");
      expect(JSON.stringify(clean)).not.toContain(id);
    }
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["resource", "event", "link"])("H1 typed session binding never exempts the same bytes on a %s", async surface => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const id = `h1-same-${surface}-correlation`;
    const tracer = new FITracer({ tracer: provider.getTracer("h1-session-surfaces") });
    tracer.startActiveSpan("origin", {}, setSession(ROOT_CONTEXT, { sessionId: id }), span => span.end());
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    const origin = privacyFilter().span(native);
    expect(origin.attributes["session.id"]).toBe(id);
    expect(privacyFilter().span(origin).attributes["session.id"]).toBe(id);
    const copied = snapshot({ "session.id": id, "control.label": "h1-public-control" });
    copied._spanContext = native.spanContext();
    if (surface === "resource") copied.resource.attributes = { "session.id": id };
    if (surface === "event") copied.events = [{ name: "event", attributes: { "session.id": id } }];
    if (surface === "link") copied.links = [{ context: native.spanContext(), attributes: { "session.id": id } }];
    const first = privacyFilter().span(copied);
    for (const clean of [first, privacyFilter().span(first)]) {
      if (surface === "resource") expect(clean.resource.attributes["session.id"]).toBe("<redacted>");
      if (surface === "event") expect(clean.events[0].attributes?.["session.id"]).toBe("<redacted>");
      if (surface === "link") expect(clean.links[0].attributes?.["session.id"]).toBe("<redacted>");
      // Nonspan credential discovery still wins over the span's name exception.
      expect(clean.attributes["session.id"]).toBe("<redacted>");
      expect(clean.attributes["control.label"]).toBe("h1-public-control");
      expect(JSON.stringify(clean)).not.toContain(id);
    }
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test("H1 logprob exemption requires provider root and cannot exempt independent credentials", () => {
  const response = { choices: [{ message: { content: "safe answer" }, logprobs: { content: [{ token: "safe token" }] } }] };
  expect(scrubProviderResponse(response).choices[0].logprobs.content[0].token).toBe("safe token");
  expect(scrubValue(response).choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(isCredentialName("choices.0.logprobs.content.0.token")).toBe(true);
  for (const path of ["secret.input_tokens", "token_count.api_key", "secret.choices.0.logprobs.content.0.token", "provider_client_secret_tokens"]) {
    expect(isCredentialName(path)).toBe(true);
    expect(scrubValue({ [path]: "h1-ancestry-private" })[path]).toBe("<redacted>");
  }
  expect(discoverSecrets([{ secret: { input_tokens: ["h1-ancestry-private"] } }])).toContain("h1-ancestry-private");
});

test.each(["normalized", "spanLimits", "generalLimits"])("H1 mapped copy reads %s provider limits without calling a tracer", source => {
  const getTracer = jest.fn(() => { throw new Error("unexpected tracer call"); });
  const provider = source === "normalized" ? { getTracer, _spanLimits: { attributeValueLengthLimit: 120 } }
    : { getTracer, _config: { [source]: { attributeValueLengthLimit: 120 } } };
  expect(trace.setGlobalTracerProvider(provider)).toBe(true);
  try {
    const payload = "AQID".repeat(16);
    const raw = snapshot({ "x.parts": JSON.stringify([{ data: payload }]), sentinel: "z".repeat(120) });
    const before = JSON.stringify(raw);
    const clean = privacyFilter().span(raw);
    expect(clean.attributes).toEqual({ "x.parts": "<unknown: host-truncated>", sentinel: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
    expect(clean.name).toBe("<unknown: host-truncated>");
    expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    expect(clean.resource.attributes).toEqual({});
    expect(clean.status).toEqual({ code: 1 });
    expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
    expect(clean.spanContext()).toEqual(raw._spanContext);
    expect(JSON.stringify(clean)).not.toContain(payload);
    expect(JSON.stringify(raw)).toBe(before);
    expect(getTracer).not.toHaveBeenCalled();
  } finally { trace.disable(); }
});

test("H1 single-pass admission retains media ancestry even when host credential names are deferred", () => {
  const attrs = { "session.audio.data": "AQID", "session.id": "h1-host-only-correlation" };
  expect(privacyFilter().admissionAttributes(attrs)).toEqual({
    "session.audio.data": "<omitted: 4 base64 chars>", "session.id": "h1-host-only-correlation",
  });
  expect(privacyFilter().attributes(attrs)["session.id"]).toBe("<redacted>");
});

test("H1 S1 short media guard preserves S6 exact signatures and ordinary text", () => {
  expect(mediaReplacement("iVBORw0K")).toBeUndefined(); // Complete-input catalog oracle is unchanged.
  expect(privacyFilter().attributes({ "gen_ai.response.latency": "iVBORw0K" })["gen_ai.response.latency"])
    .toBe("<omitted: 8 base64 chars>");
  const ordinary = { text: "ordinary answer", riff: "UklGRgAAAAA=", hidden: "__REDACTED__", short: "iVBO", unrelated: "AQID".repeat(16) };
  expect(privacyFilter().attributes(ordinary)).toEqual(ordinary);
});

test("H1 marker leads the exact no-content boundary but canonical output retains F1 ordering", () => {
  const base = { "gen_ai.span.kind": "LLM", "output.value": "safe answer", "output.mime_type": "text/plain" };
  const full = projectAttributes(base, 2);
  expect(Object.keys(full.attributes)).toEqual([DROPPED_CONTENT_COUNT, "gen_ai.span.kind"]);
  expect(full.attributes[DROPPED_CONTENT_COUNT]).toBe(2);
  expect(full.dropped).toBe(2);
  expect(projectAttributes(Object.fromEntries(Object.entries(base).reverse()), 2)).toEqual(full);
  expect(projectAttributes(base, 3).attributes).toEqual(base);
});

test.each(["registered", "shape", "media", "truncated", "override"])("H1 typed session never bypasses %s guards", async kind => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: kind === "truncated" ? 8 : 4096 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const id = kind === "shape" ? "sk-" + "Q".repeat(24) : kind === "media" ? "iVBORw0KGgoAAAANSUhEUg==" : `h1-${kind}-correlation-value`;
    const tracer = new FITracer({ tracer: provider.getTracer("h1-session-guards") });
    tracer.startActiveSpan("guarded", kind === "override" ? { attributes: { "session.id": "h1-caller-override" } } : {}, setSession(ROOT_CONTEXT, { sessionId: id }), span => span.end());
    if (kind === "registered") registerSecret(id); // Credentials discovered after admission still win.
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    const first = privacyFilter().span(native);
    for (const clean of [first, privacyFilter().span(first)]) {
      expect(clean.attributes["session.id"]).toBe("<redacted>");
      expect(JSON.stringify(clean)).not.toContain(id);
    }
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});
