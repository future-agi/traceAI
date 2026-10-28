import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { Agent } from "@voltagent/core";
import { FIVoltAgentSpanProcessor } from "../FIVoltAgentSpanProcessor";
import { MARKERS, RecordingProcessor, approvalWorkflow, attrs, fiProvider,
  passThroughGuardrail, resetOtelGlobals, scriptedModel, settle, voltagent } from "./helpers";

let observability: ReturnType<typeof voltagent> | undefined;
afterEach(async () => {
  await resetOtelGlobals(observability);
  observability = undefined;
});
const HIDDEN = { hideInputs: true, hideOutputs: true };

// Isolated by jest.resume.config.cjs: upstream @voltagent/core 2.11.0 retains a
// referenced 500 ms interval on completed resumed steps even after app.shutdown().
// All assertions and teardown still run. Only the dedicated runner exits after Jest.
describe("span events from a real agent and workflow (M1)", () => {
  async function runEventJourney(hidden: boolean) {
    const { exporter, provider } = fiProvider();
    const recorder = new RecordingProcessor();
    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider, traceConfig: hidden ? HIDDEN : undefined });
    const { model } = scriptedModel("mockai/mock-model-1", [{ kind: "text", text: MARKERS.answer, input: 3, output: 2 }]);
    const agent = new Agent({ name: "assistant", instructions: "x", model, outputGuardrails: [passThroughGuardrail()] });
    const workflow = approvalWorkflow();
    observability = voltagent({ assistant: agent }, [recorder, processor], { approval: workflow });

    const stream = await agent.streamText(MARKERS.prompt, { conversationId: "conv-events" });
    expect(await stream.text).toBe(MARKERS.answer);
    const suspended = await workflow.run({ amount: 5, question: MARKERS.suspendData });
    expect(suspended.status).toBe("suspended");
    const resumed = await suspended.resume({ approved: true, note: MARKERS.resumeData });
    expect(resumed.status).toBe("completed");
    await settle(recorder);
    await processor.forceFlush();

    const exported = exporter.getFinishedSpans();
    const events = (spans: ReadableSpan[], name: string) =>
      spans.flatMap((span) => span.events.filter((event) => event.name === name).map((event) => event.attributes ?? {}));
    // Control: VoltAgent really wrote the content into the events the other processors received.
    expect(events(recorder.spans, "guardrail.stream.process").map((a) => a["guardrail.chunk.text"])).toContain(
      MARKERS.answer,
    );
    expect(String(events(recorder.spans, "workflow.suspended")[0]["suspension.data"])).toContain(MARKERS.suspendData);
    expect(String(events(recorder.spans, "workflow.resumed")[0]["resume.data"])).toContain(MARKERS.resumeData);
    expect(exported).toHaveLength(recorder.spans.length);
    return { exported, events: (name: string) => events(exported, name) };
  }

  it("drops streamed guardrail text and workflow suspend/resume payloads from events when hidden", async () => {
    const { exported, events } = await runEventJourney(true);
    const processed = events("guardrail.stream.process");
    expect(processed.map((a) => a["guardrail.chunk.type"])).toContain("text-delta");
    for (const a of processed) expect(a["guardrail.chunk.text"]).toBeUndefined();
    expect(events("guardrail.stream.start")).toHaveLength(1);
    const [suspended] = events("workflow.suspended");
    expect(suspended).toMatchObject({ "suspension.step_index": 0 });
    expect(suspended["suspension.reason"]).toBeUndefined(); // free text, hidden with inputs/outputs
    expect(suspended["suspension.data"]).toBeUndefined();
    expect(suspended["suspension.checkpoint"]).toBeUndefined();
    const [resumed] = events("workflow.resumed");
    expect(resumed).toEqual({ "resume.step_index": 0 });
    for (const span of exported.filter((s) => s.name === "workflow.approval")) {
      expect(attrs(span)["fi.span.kind"]).toBe("CHAIN");
    }
    const blob = JSON.stringify(exported.map((span) => [span.name, span.attributes, span.events]));
    for (const marker of [MARKERS.answer, MARKERS.prompt, MARKERS.suspendData, MARKERS.resumeData]) {
      expect(blob).not.toContain(marker);
    }
  });

  it("keeps them by default (control run)", async () => {
    const { events } = await runEventJourney(false);
    expect(events("guardrail.stream.process").map((a) => a["guardrail.chunk.text"])).toContain(MARKERS.answer);
    const [suspended] = events("workflow.suspended");
    expect(String(suspended["suspension.data"])).toContain(MARKERS.suspendData);
    expect(suspended["suspension.checkpoint"]).toEqual(expect.any(String));
    expect(String(events("workflow.resumed")[0]["resume.data"])).toContain(MARKERS.resumeData);
  });
});
