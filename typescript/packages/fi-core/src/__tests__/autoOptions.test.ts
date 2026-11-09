import { AlwaysOffSampler, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { register, createSpanExporter } from "../otel";
import { ProjectType } from "../fi_types";

describe("additive auto-instrumentation provider options", () => {
  it("uses supplied processors, does no startup I/O and preserves the experiment default", async () => {
    const exporter = new InMemorySpanExporter();
    const fetch = jest.spyOn(globalThis, "fetch");
    const signals = [process.listenerCount("SIGINT"), process.listenerCount("SIGTERM")];
    const provider = register({ projectName: "auto-options", setGlobalTracerProvider: false, spanProcessors: [new SimpleSpanProcessor(exporter)] });
    provider.getTracer("test").startSpan("test").end(); await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].resource.attributes.project_type).toBe(ProjectType.EXPERIMENT);
    expect(fetch).not.toHaveBeenCalled();
    expect([process.listenerCount("SIGINT"), process.listenerCount("SIGTERM")]).toEqual(signals);
    await provider.shutdown(); fetch.mockRestore();
  });
  it("honors the supplied sampler without registering globally", async () => {
    const exporter = new InMemorySpanExporter();
    const provider = register({ projectName: "auto-options", projectType: ProjectType.OBSERVE, setGlobalTracerProvider: false, sampler: new AlwaysOffSampler(), spanProcessors: [new SimpleSpanProcessor(exporter)] });
    provider.getTracer("test").startSpan("not-sampled").end(); await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(0); await provider.shutdown();
  });
  it("constructs a standard exporter without exporting at startup", async () => {
    const fetch = jest.spyOn(globalThis, "fetch");
    const exporter = createSpanExporter({ endpoint: "http://127.0.0.1:1", headers: { "x-api-key": "mock" } });
    expect(exporter).toBeDefined(); expect(fetch).not.toHaveBeenCalled();
    await exporter.shutdown(); fetch.mockRestore();
  });
});
