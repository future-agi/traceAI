import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { FITracer } from "../trace/trace-config/FITracer";

// Lead control (RL 2193, disclosed): LIMIT-SURFACES-r2 and EVIDENCE-CLASS-r2. The host default resource
// service.name (unknown_service:<argv0>, at least 16 characters) at a value limit of 16 is class (b)
// host-loss evidence, so the lineage is withheld by design (over-redaction, disclosed).
const savedEnv = { ...process.env }, registryKey = Symbol.for("traceai.secrets");
beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  Reflect.deleteProperty(globalThis, registryKey);
});
afterEach(() => { process.env = { ...savedEnv }; Reflect.deleteProperty(globalThis, registryKey); });

test("R10 lead: default host resource at value limit 16 withholds the lineage (class b, by design)", async () => {
  const memory = new InMemorySpanExporter(), provider = new BasicTracerProvider({
    spanLimits: { attributeCountLimit: 32, attributeValueLengthLimit: 16 }, spanProcessors: [new SimpleSpanProcessor(memory)],
  });
  try {
    new FITracer({ tracer: provider.getTracer("r10 lead") }).startSpan("text", { attributes: { "output.value": "ordinary answer", "output.mime_type": "text/plain" } }).end();
    const raw: any = memory.getFinishedSpans()[0];
    expect(String(raw.resource.attributes["service.name"]).length).toBeGreaterThanOrEqual(16);
    const clean: any = privacyFilter().span(raw);
    expect(clean.attributes["output.value"]).toBe("<unknown: host-truncated>");
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  } finally { await provider.shutdown(); }
});
