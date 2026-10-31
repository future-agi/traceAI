import { trace } from "@opentelemetry/api";
import { Agent } from "@voltagent/core";
import { FIVoltAgentSpanProcessor } from "../FIVoltAgentSpanProcessor";
import { RecordingProcessor, fiProvider, resetOtelGlobals, scriptedModel, voltagent } from "./helpers";

let observability: ReturnType<typeof voltagent> | undefined;
afterEach(async () => {
  await resetOtelGlobals(observability);
});

describe("register() with setGlobalTracerProvider left on", () => {
  it("warns, because VoltAgent's spans then bypass every ObservabilityConfig processor", async () => {
    const warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
    const { exporter, provider } = fiProvider();
    trace.setGlobalTracerProvider(provider); // what register() does by default

    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider });
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("setGlobalTracerProvider: false"));

    // The failure mode the warning describes, reproduced with VoltAgent 2.11.0.
    const recorder = new RecordingProcessor();
    const { model } = scriptedModel("mockai/mock-model-1", [{ kind: "text", text: "ok", input: 1, output: 1 }]);
    const agent = new Agent({ name: "assistant", instructions: "x", model });
    observability = voltagent({ assistant: agent }, [recorder, processor]);
    await agent.generateText("hi");
    await processor.forceFlush();

    expect(recorder.spans).toHaveLength(0); // VoltOps / user processors see nothing
    const raw = exporter.getFinishedSpans();
    expect(raw.length).toBeGreaterThan(0); // spans went straight to the FI provider...
    expect(raw.every((span) => span.attributes["fi.span.kind"] === undefined)).toBe(true); // ...unmapped
    warn.mockRestore();
  });
});
