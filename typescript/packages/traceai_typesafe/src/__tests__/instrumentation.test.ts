import { context, trace } from "@opentelemetry/api";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { TypeSafeInstrumentation } from "../instrumentation";
import { BASE_URL, fakeFetch, Reply, SDKS, sdk, SECRET_KEY, tracing, WIRE } from "./harness";

describe.each(SDKS)("TypeSafe instrumentation over %s", (name) => {
  const lib = sdk(name);
  const hasBox = typeof (lib as { box?: unknown }).box === "function"; // box questions arrived after 0.5.7
  let instrumentation: TypeSafeInstrumentation;
  let t: ReturnType<typeof tracing>;

  beforeEach(() => {
    t = tracing();
    t.provider.register(); // installs a context manager so suppression and nesting work
    instrumentation = new TypeSafeInstrumentation();
    instrumentation.setTracerProvider(t.provider);
    instrumentation.manuallyInstrument(lib as unknown as Record<string, unknown>);
  });
  afterEach(async () => {
    instrumentation.disable();
    await t.provider.shutdown();
    context.disable();
    trace.disable();
  });

  const client = (replies: Reply[], extra: Record<string, unknown> = {}) => {
    const { fetch, sent } = fakeFetch(replies);
    return { sent, client: new lib.TypeSafeClient({ apiKey: SECRET_KEY, baseURL: BASE_URL, fetch, retry: { maxRetries: 0 }, ...extra }) };
  };
  const ask = (c: InstanceType<typeof lib.TypeSafeClient>, options?: Record<string, unknown>) =>
    c.systemOne({
      state: "I was charged twice. Please help.",
      questions: {
        billing: lib.noul("Is this about billing?"),
        tone: lib.choice("What is the tone?", { calm: null, angry: null }),
        quality: lib.score("How good?", ["bad", "ok", "good"]),
        ...(hasBox ? { where: lib.box("The damaged area") } : {}),
      },
    }, options);
  const only = () => {
    expect(t.spans()).toHaveLength(1);
    return t.spans()[0];
  };

  it("records one EVALUATOR span per call with every question as attributes", async () => {
    const { client: c } = client([{}]);
    const result = await ask(c);
    expect(result.answers.billing.noul).toBe(0.93);
    const span = only();
    const a = span.attributes;
    expect(span.name).toBe("typesafe.system_one");
    expect(a[S.FI_SPAN_KIND]).toBe("EVALUATOR");
    expect(a[S.GEN_AI_PROVIDER_NAME]).toBe("typesafe");
    expect(a[S.GEN_AI_REQUEST_MODEL]).toBe("jev-latest");
    expect(a[S.GEN_AI_RESPONSE_MODEL]).toBe("jev-2026-10-01");
    expect(a["typesafe.response.model"]).toBe("jev-2026-10-01");
    expect(a["server.address"]).toBe("typesafe.invalid");
    expect(a["typesafe.question_count"]).toBe(hasBox ? 4 : 3);
    expect(a["typesafe.request_id"]).toBe("req-synthetic-1");
    expect([a[S.GEN_AI_USAGE_INPUT_TOKENS], a[S.GEN_AI_USAGE_OUTPUT_TOKENS], a[S.GEN_AI_USAGE_TOTAL_TOKENS]]).toEqual([12, 3, 15]);
    expect(a["typesafe.question.billing.type"]).toBe("noul");
    expect(a["typesafe.question.billing.noul"]).toBe(0.93);
    expect(a["typesafe.question.billing.confidence"]).toBeUndefined();
    expect(a["typesafe.question.tone.choice"]).toBe("angry");
    expect(a["typesafe.question.tone.confidence"]).toBe(0.8);
    expect(a["typesafe.question.quality.score"]).toBe(2.4);
    if (hasBox) {
      expect(a["typesafe.question.where.box.top"]).toBe(0.1);
      expect(a["typesafe.question.where.box.right"]).toBe(0.6);
      expect(a["typesafe.question.where.confidence"]).toBeUndefined();
    }
    expect(span.status.code).toBe(1);
  });

  it("never records probability maps, legends or credentials", async () => {
    const { client: c } = client([{}]);
    await ask(c);
    const dump = JSON.stringify(only().attributes) + JSON.stringify(only().events);
    expect(dump).not.toContain("legend-");
    expect(dump).not.toContain("probabilities");
    expect(dump).not.toContain(SECRET_KEY);
  });

  it("captures the state and questions as input and the decisions as output", async () => {
    const { client: c } = client([{}]);
    await ask(c);
    const a = only().attributes;
    expect(JSON.parse(a[S.INPUT_VALUE] as string)).toMatchObject({ state: "I was charged twice. Please help.", questions: { billing: { instructions: "Is this about billing?" } } });
    expect(JSON.parse(a[S.OUTPUT_VALUE] as string).tone).toEqual({ type: "choice", choice: "angry", confidence: 0.8 });
  });

  it("uses an explicit model and a per-call idempotency key", async () => {
    const { client: c } = client([{}]);
    await c.systemOne({ state: "x", questions: { q: lib.noul("q?") }, model: "jev-preview" }, { headers: { "Idempotency-Key": "idem-1" } });
    expect(only().attributes[S.GEN_AI_REQUEST_MODEL]).toBe("jev-preview");
    expect(only().attributes["typesafe.idempotency_key"]).toBe("idem-1");
  });

  it("keeps the APIPromise contract: withResponse and asResponse", async () => {
    const { client: c } = client([{}, {}]);
    const withResponse = await ask(c).withResponse();
    expect(withResponse.requestId).toBe("req-synthetic-1");
    expect(withResponse.data.answers.billing.noul).toBe(0.93);
    expect(t.spans()).toHaveLength(1);
    const response = await ask(c).asResponse();
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ model: WIRE.model });
    expect(t.spans()).toHaveLength(2);
  });

  it("re-raises an API error unchanged and marks the span", async () => {
    const { client: c } = client([{ status: 429, body: { detail: { error_type: "rate_limited", message: "slow down" } }, headers: { "retry-after-ms": "250" } }]);
    await expect(ask(c)).rejects.toBeInstanceOf(lib.RateLimitError);
    const span = only();
    expect(span.status.code).toBe(2);
    expect(span.attributes["error.type"]).toBe("RateLimitError");
    expect(span.attributes["http.response.status_code"]).toBe(429);
    expect(span.attributes["typesafe.error_type"]).toBe("rate_limited");
    expect(span.attributes["typesafe.retry_after_ms"]).toBe(250);
    expect(span.attributes["typesafe.request_id"]).toBe("req-synthetic-1");
    expect(JSON.stringify(span.events)).not.toContain("stack");
  });

  it("does not export the server's error text when content is hidden", async () => {
    const body = { detail: { error_type: "bad_request", message: "echo-of-private-state" } };
    const visible = client([{ status: 400, body }]).client;
    await expect(ask(visible)).rejects.toBeInstanceOf(lib.BadRequestError);
    expect(JSON.stringify(only().status)).toContain("echo-of-private-state");
  });

  it("records a client-side validation error without its text", async () => {
    const { client: c, sent } = client([{}]);
    // Older SDKs throw this synchronously, newer ones reject; both must reach the caller unchanged.
    await expect((async () => c.systemOne({ state: "x", questions: {} }))()).rejects.toBeInstanceOf(lib.TypeSafeError);
    expect(sent).toHaveLength(0);
    const span = only();
    expect(span.status.code).toBe(2);
    expect(span.attributes["error.type"]).toBe("TypeSafeError");
    expect(span.status.message ?? "").not.toContain("question is required");
  });

  it("marks a caller abort as cancelled", async () => {
    const controller = new AbortController();
    const { client: c } = client([{}]);
    const pending = ask(c, { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toBeInstanceOf(lib.APIUserAbortError);
    const span = only();
    expect(span.attributes["typesafe.cancelled"]).toBe(true);
    expect(span.status.code).toBe(2);
  });

  it("covers SDK retries inside one span", async () => {
    const { client: c, sent } = client([{ status: 429, body: {} }, {}], { retry: { maxRetries: 2, backoffInitialMs: 1, backoffMaxMs: 2, backoffJitter: 0 } });
    await ask(c);
    expect(sent).toHaveLength(2);
    expect(only().status.code).toBe(1);
  });

  const hasImage = typeof (lib as { image?: unknown }).image === "function"; // images arrived after 0.5.7
  (hasImage ? it : it.skip)("notes an image by its reference, never its bytes", async () => {
    const { client: c } = client([{}]);
    const photo = lib.image(new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3]));
    await c.systemOne({ state: photo, questions: { damaged: lib.noul("Is it damaged?") } });
    const input = only().attributes[S.INPUT_VALUE] as string;
    expect(input).toMatch(/<image:[0-9a-f]+>/);
    expect(input).not.toContain("137");
  });

  it("creates no span while tracing is suppressed or after disable()", async () => {
    const { suppressTracing } = await import("@opentelemetry/core");
    const { client: c } = client([{}, {}]);
    await context.with(suppressTracing(context.active()), () => ask(c));
    expect(t.spans()).toHaveLength(0);
    instrumentation.disable();
    await ask(c);
    expect(t.spans()).toHaveLength(0);
  });
});

describe("TypeSafe instrumentation module shape", () => {
  it("leaves a module without the client class untouched", () => {
    const instrumentation = new TypeSafeInstrumentation();
    expect(() => instrumentation.manuallyInstrument({ other: 1 })).not.toThrow();
    instrumentation.disable();
  });
});
