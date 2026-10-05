import { SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { ExportResultCode } from "@opentelemetry/core";
import { InMemorySpanExporter, type ReadableSpan, type SpanExporter } from "@opentelemetry/sdk-trace-base";
import { Agent } from "@voltagent/core";
import { z } from "zod";
import { FIVoltAgentSpanProcessor } from "../FIVoltAgentSpanProcessor";
import { isPromotedUsageKey } from "../mapping";
import {
  FI_RESOURCE_ATTRIBUTES,
  MARKERS,
  RecordingProcessor,
  approvalWorkflow,
  attrs,
  byName,
  explodingTool,
  fiProvider,
  one,
  parentId,
  passThroughGuardrail,
  promotedInputTokens,
  resetOtelGlobals,
  scriptedModel,
  settle,
  toolCallingSteps,
  voltagent,
  weatherTool,
} from "./helpers";

type Observability = ReturnType<typeof voltagent>;
let observability: Observability | undefined;

afterEach(async () => {
  await resetOtelGlobals(observability);
  observability = undefined;
});

async function runToolCallingAgent(options: { captureContent?: boolean } = {}) {
  const { exporter, provider } = fiProvider();
  const recorder = new RecordingProcessor();
  const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider, ...options });
  const { model, reported } = scriptedModel("mockai/mock-model-1", toolCallingSteps());
  const agent = new Agent({
    name: "assistant",
    instructions: MARKERS.instructions,
    model,
    tools: [weatherTool, explodingTool],
  });
  observability = voltagent({ assistant: agent }, [recorder, processor]);

  const result = await agent.generateText(MARKERS.prompt, {
    conversationId: "conv-123",
    userId: "user-9",
    maxSteps: 4,
  });
  // VoltAgent ends some memory spans in the background after generateText resolves.
  await settle(recorder);
  await processor.forceFlush();
  return { result, exported: exporter.getFinishedSpans(), recorded: recorder.spans, reported, observability };
}

describe("FIVoltAgentSpanProcessor with a real VoltAgent agent (AC-01)", () => {
  it("exports a mapped copy of every VoltAgent span with kinds, parenting, model, session and errors", async () => {
    const { result, exported } = await runToolCallingAgent();
    expect(result.text).toBe(MARKERS.answer);

    const root = one(exported, "assistant");
    const llm = one(exported, "llm:generateText");
    const weather = one(exported, "tool.execution:get_weather");
    const failing = one(exported, "tool.execution:explode");

    expect(new Set(exported.map((span) => span.spanContext().traceId)).size).toBe(1);
    expect(parentId(root)).toBeUndefined();
    for (const child of [llm, weather, failing]) expect(parentId(child)).toBe(root.spanContext().spanId);

    expect(attrs(root)["fi.span.kind"]).toBe("AGENT");
    expect(attrs(llm)["fi.span.kind"]).toBe("LLM");
    expect(attrs(weather)["fi.span.kind"]).toBe("TOOL");
    expect(attrs(failing)["gen_ai.span.kind"]).toBe("TOOL");
    for (const span of byName(exported, "memory.read")) expect(attrs(span)["fi.span.kind"]).toBe("RETRIEVER");
    for (const span of byName(exported, "memory.write")) expect(attrs(span)["fi.span.kind"]).toBe("CHAIN");
    expect(llm.kind).toBe(SpanKind.CLIENT);

    expect(attrs(llm)["gen_ai.request.model"]).toBe("mockai/mock-model-1");
    expect(attrs(llm)["gen_ai.provider.name"]).toBe("mockai");
    expect(attrs(root)["gen_ai.request.model"]).toBe("mockai/mock-model-1");
    expect(attrs(weather)["gen_ai.tool.name"]).toBe("get_weather");
    expect(attrs(weather)["gen_ai.tool.call.id"]).toBe("call-weather");

    for (const span of exported) {
      expect(attrs(span)["session.id"]).toBe("conv-123");
      expect(attrs(span)["user.id"]).toBe("user-9");
      expect(span.resource.attributes).toMatchObject(FI_RESOURCE_ATTRIBUTES);
    }

    expect(failing.status.code).toBe(SpanStatusCode.ERROR);
    expect(failing.events.map((event) => event.name)).toContain("exception");
    expect(attrs(failing)["error.message"]).toBe("tool exploded");
    expect(weather.status.code).toBe(SpanStatusCode.OK);
    expect(root.status.code).toBe(SpanStatusCode.OK);
  });

  it("keeps promoted token keys on model-call spans only, summing to the model calls", async () => {
    const { exported, reported } = await runToolCallingAgent();
    expect(reported).toHaveLength(2); // two model steps: tool calls, then the answer
    const modelCallInputTokens = reported.reduce((sum, step) => sum + step.input, 0);
    const modelCallOutputTokens = reported.reduce((sum, step) => sum + step.output, 0);

    expect(promotedInputTokens(exported)).toBe(modelCallInputTokens);
    for (const span of exported) {
      if (attrs(span)["fi.span.kind"] === "LLM") continue;
      for (const key of Object.keys(attrs(span))) expect(isPromotedUsageKey(key)).toBe(false);
    }

    const llm = attrs(one(exported, "llm:generateText"));
    expect(llm["gen_ai.usage.input_tokens"]).toBe(modelCallInputTokens);
    expect(llm["gen_ai.usage.output_tokens"]).toBe(modelCallOutputTokens);
    expect(llm["voltagent.llm.last_step_usage.prompt_tokens"]).toBe(reported[1].input);
    expect(llm["voltagent.usage.reconciled"]).toBe(true);
    const root = attrs(one(exported, "assistant"));
    expect(root["voltagent.usage.input_tokens"]).toBe(modelCallInputTokens);
    expect(root["usage.prompt_tokens"]).toBe(modelCallInputTokens);
  });

  it("exports no content by default", async () => {
    const { exported } = await runToolCallingAgent();
    const blob = JSON.stringify(exported.map((span) => [span.name, span.attributes, span.events]));
    for (const marker of Object.values(MARKERS)) expect(blob).not.toContain(marker);
    for (const span of exported) {
      for (const key of ["input", "output", "input.value", "output.value", "llm.messages", "agent.instructions"]) {
        expect(attrs(span)[key]).toBeUndefined();
      }
    }
    // Exception events keep exception.type / .message / .stacktrace (README: passed through).
    const exception = one(exported, "tool.execution:explode").events.find((event) => event.name === "exception");
    expect(exception?.attributes?.["exception.message"]).toBe("tool exploded");
    expect(exception?.attributes?.["exception.type"]).toEqual(expect.any(String));
    expect(exception?.attributes?.["exception.stacktrace"]).toEqual(expect.stringContaining("tool exploded"));
  });

  it("exports content only after captureContent: true (control run)", async () => {
    const { exported } = await runToolCallingAgent({ captureContent: true });
    const root = attrs(one(exported, "assistant"));
    expect(root["input.value"]).toBe(MARKERS.prompt);
    expect(root["output.value"]).toBe(MARKERS.answer);
    const weather = attrs(one(exported, "tool.execution:get_weather"));
    expect(String(weather["input.value"])).toContain(MARKERS.toolArg);
    expect(String(weather["output.value"])).toContain(MARKERS.toolResult);
  });
});

describe("streamText", () => {
  it("maps the streaming path and reconciles its multi-step usage", async () => {
    const { exporter, provider } = fiProvider();
    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider });
    const { model, reported } = scriptedModel("mockai/mock-model-1", toolCallingSteps());
    const agent = new Agent({ name: "assistant", instructions: "x", model, tools: [weatherTool, explodingTool] });
    observability = voltagent({ assistant: agent }, [processor]);
    const stream = await agent.streamText(MARKERS.prompt, { conversationId: "conv-stream", maxSteps: 4 });
    expect(await stream.text).toBe(MARKERS.answer);
    await new Promise((resolve) => setTimeout(resolve, 50));
    await processor.forceFlush();
    const spans = exporter.getFinishedSpans();
    const llm = attrs(one(spans, "llm:streamText"));
    expect(reported).toHaveLength(2);
    expect(promotedInputTokens(spans)).toBe(reported.reduce((sum, step) => sum + step.input, 0));
    expect(llm["fi.span.kind"]).toBe("LLM");
    expect(attrs(one(spans, "assistant"))["session.id"]).toBe("conv-stream");
    expect(JSON.stringify(spans.map((span) => span.attributes))).not.toContain(MARKERS.prompt);
  });
});

describe("coexistence with other processors in the same array (AC-06)", () => {
  it("every processor still receives every span, unchanged, and our export count matches", async () => {
    const { exported, recorded, observability: obs } = await runToolCallingAgent();
    expect(recorded.length).toBeGreaterThan(0);
    expect(exported).toHaveLength(recorded.length);
    expect(exported.map((span) => span.spanContext().spanId).sort()).toEqual(
      recorded.map((span) => span.spanContext().spanId).sort(),
    );

    // VoltAgent's own local storage processor (the VoltOps console's source) saw the same spans.
    const traceId = recorded[0].spanContext().traceId;
    const stored = await obs.getTraceFromStorage(traceId);
    expect(stored).toHaveLength(recorded.length);

    // The span other processors hold is the original: no FI keys added, VoltAgent keys intact.
    const originalRoot = one(recorded, "assistant");
    expect(attrs(originalRoot)["fi.span.kind"]).toBeUndefined();
    expect(attrs(originalRoot).input).toBe(MARKERS.prompt);
    expect(attrs(originalRoot)["usage.prompt_tokens"]).toBe(24);
    const originalLLM = one(recorded, "llm:generateText");
    expect(attrs(originalLLM)["llm.usage.prompt_tokens"]).toBe(13);
    expect(originalRoot.resource.attributes["service.name"]).toBe("voltagent");
  });
});

describe("subagents (AC-04)", () => {
  it("puts the subagent in the supervisor's trace under the delegate_task tool span", async () => {
    const { exporter, provider } = fiProvider();
    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider });
    const child = scriptedModel("mockai/child-model", [{ kind: "text", text: "child answer", input: 5, output: 3 }]);
    const supervisorModel = scriptedModel("mockai/supervisor-model", [
      {
        kind: "tools",
        calls: [
          {
            id: "call-delegate",
            name: "delegate_task",
            args: { task: MARKERS.subtask, targetAgents: ["researcher"] },
          },
        ],
        input: 20,
        output: 9,
      },
      { kind: "text", text: "final", input: 30, output: 4 },
    ]);
    const researcher = new Agent({ name: "researcher", instructions: "research", model: child.model });
    const supervisor = new Agent({
      name: "supervisor",
      instructions: "delegate",
      model: supervisorModel.model,
      subAgents: [researcher],
    });
    observability = voltagent({ supervisor, researcher }, [processor]);

    await supervisor.generateText("go", { conversationId: "conv-sub", userId: "user-1", maxSteps: 4 });
    await processor.forceFlush();
    const spans = exporter.getFinishedSpans();
    expect(child.reported).toHaveLength(1); // the subagent really ran (through streamText)
    expect(supervisorModel.reported).toHaveLength(2);

    expect(new Set(spans.map((span) => span.spanContext().traceId)).size).toBe(1);
    const root = one(spans, "supervisor");
    const delegate = one(spans, "tool.execution:delegate_task");
    const subagent = spans.find((span) => span.name.startsWith("subagent:"));
    expect(subagent).toBeDefined();
    expect(parentId(root)).toBeUndefined();
    expect(parentId(delegate)).toBe(root.spanContext().spanId);
    expect(parentId(subagent as ReadableSpan)).toBe(delegate.spanContext().spanId);
    expect(attrs(subagent as ReadableSpan)["fi.span.kind"]).toBe("AGENT");
    expect((subagent as ReadableSpan).status.code).toBe(SpanStatusCode.OK);
    expect(delegate.status.code).toBe(SpanStatusCode.OK);
    const childLLM = spans.filter(
      (span) => attrs(span)["fi.span.kind"] === "LLM" && attrs(span)["llm.model"] === "mockai/child-model",
    );
    expect(childLLM).toHaveLength(1);
    expect(attrs(childLLM[0])["llm.operation"]).toBe("streamText");

    const modelCalls = [...supervisorModel.reported, ...child.reported].reduce((sum, step) => sum + step.input, 0);
    expect(promotedInputTokens(spans)).toBe(modelCalls);
    expect(JSON.stringify(spans.map((span) => span.attributes))).not.toContain(MARKERS.subtask);
  });
});

describe("failure isolation", () => {
  class ThrowingExporter implements SpanExporter {
    export(): void {
      throw new Error("collector down");
    }
    async shutdown(): Promise<void> {
      throw new Error("shutdown failed");
    }
    async forceFlush(): Promise<void> {
      throw new Error("flush failed");
    }
  }

  it("an exporter that throws never breaks the agent, flush or shutdown", async () => {
    const processor = new FIVoltAgentSpanProcessor({ exporter: new ThrowingExporter(), batch: false });
    const { model } = scriptedModel("mockai/mock-model-1", toolCallingSteps());
    const agent = new Agent({ name: "assistant", instructions: "x", model, tools: [weatherTool, explodingTool] });
    observability = voltagent({ assistant: agent }, [processor]);
    const result = await agent.generateText("hi", { maxSteps: 4 });
    expect(result.text).toBe(MARKERS.answer);
    await expect(processor.forceFlush()).resolves.toBeUndefined();
    await expect(processor.shutdown()).resolves.toBeUndefined();
    await expect(processor.shutdown()).resolves.toBeUndefined();
  });

  it("an exporter that reports FAILED never breaks flush", async () => {
    const failing: SpanExporter = {
      export: (_spans, done) => done({ code: ExportResultCode.FAILED, error: new Error("503") }),
      shutdown: async () => undefined,
    };
    const processor = new FIVoltAgentSpanProcessor({ exporter: failing });
    const { model } = scriptedModel("mockai/mock-model-1", [{ kind: "text", text: "ok", input: 1, output: 1 }]);
    const agent = new Agent({ name: "assistant", instructions: "x", model });
    observability = voltagent({ assistant: agent }, [processor]);
    await expect(agent.generateText("hi")).resolves.toMatchObject({ text: "ok" });
    await expect(processor.forceFlush()).resolves.toBeUndefined();
    await expect(processor.shutdown()).resolves.toBeUndefined();
  });

  it("ignores spans after shutdown and requires a destination", async () => {
    expect(() => new FIVoltAgentSpanProcessor({})).toThrow(/tracerProvider/);
    const exporter = new InMemorySpanExporter();
    const processor = new FIVoltAgentSpanProcessor({ exporter, batch: false });
    await processor.shutdown();
    const { exporter: other, provider } = fiProvider();
    const live = new FIVoltAgentSpanProcessor({ tracerProvider: provider });
    const recorder = new RecordingProcessor();
    const { model } = scriptedModel("mockai/mock-model-1", [{ kind: "text", text: "ok", input: 1, output: 1 }]);
    const agent = new Agent({ name: "assistant", instructions: "x", model });
    observability = voltagent({ assistant: agent }, [recorder, processor, live]);
    await agent.generateText("hi");
    await settle(recorder);
    await live.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(other.getFinishedSpans().length).toBe(recorder.spans.length);
  });
});

describe("batching through the register() provider's exporter", () => {
  it("sends one batch on flush, and shutdown leaves the provider's exporter usable", async () => {
    const { exporter, provider } = fiProvider();
    const exportCalls: number[] = [];
    const originalExport = exporter.export.bind(exporter);
    exporter.export = (spans, done) => {
      exportCalls.push(spans.length);
      originalExport(spans, done);
    };
    const processor = new FIVoltAgentSpanProcessor({
      tracerProvider: provider,
      batchConfig: { scheduledDelayMillis: 60_000 },
    });
    const recorder = new RecordingProcessor();
    const { model } = scriptedModel("mockai/mock-model-1", toolCallingSteps());
    const agent = new Agent({ name: "assistant", instructions: "x", model, tools: [weatherTool, explodingTool] });
    observability = voltagent({ assistant: agent }, [recorder, processor]);
    await agent.generateText("hi", { maxSteps: 4 });
    await settle(recorder);
    expect(exportCalls).toEqual([]); // nothing sent per span
    await processor.forceFlush();
    expect(exportCalls).toEqual([recorder.spans.length]);
    await processor.shutdown();
    // The exporter still belongs to the provider: other instrumentations keep exporting.
    provider.getTracer("other").startSpan("after-shutdown").end();
    await provider.forceFlush();
    expect(exporter.getFinishedSpans().map((span) => span.name)).toContain("after-shutdown");
  });

  it("can export per span instead (batch: false)", async () => {
    const { exporter, provider } = fiProvider();
    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider, batch: false });
    const { model } = scriptedModel("mockai/mock-model-1", [{ kind: "text", text: "ok", input: 1, output: 1 }]);
    const agent = new Agent({ name: "assistant", instructions: "x", model });
    const recorder = new RecordingProcessor();
    observability = voltagent({ assistant: agent }, [recorder, processor]);
    await agent.generateText("hi");
    await settle(recorder);
    expect(exporter.getFinishedSpans()).toHaveLength(recorder.spans.length);
  });
});

/** A finished span as VoltAgent hands it to processors, with the given attributes and events. */
function fakeSpan(
  name: string,
  attributes: Record<string, unknown>,
  events: Array<{ name: string; attributes?: Record<string, unknown> }>,
): ReadableSpan {
  return {
    name,
    kind: SpanKind.INTERNAL,
    spanContext: () => ({ traceId: "c".repeat(32), spanId: name.length.toString(16).padStart(16, "d"), traceFlags: 1 }),
    parentSpanContext: undefined,
    startTime: [0, 0],
    endTime: [0, 10],
    status: { code: SpanStatusCode.OK },
    attributes,
    links: [],
    events: events.map((event, index) => ({ ...event, time: [0, index + 1] })),
    duration: [0, 10],
    ended: true,
    resource: { attributes: {} },
    instrumentationScope: { name: "@voltagent/core" },
    droppedAttributesCount: 0,
    droppedEventsCount: 0,
    droppedLinksCount: 0,
  } as unknown as ReadableSpan;
}

describe("span events (M1)", () => {
  // Event shapes VoltAgent 2.11.0 writes: output-guardrail-stream-runner.ts (guardrail.stream.*),
  // workflow/open-telemetry/trace-context.ts (workflow.suspended / workflow.resumed), and the
  // OpenTelemetry SDK's recordException.
  const guardrailSpan = () =>
    fakeSpan("guardrail.output.stream.1", { "span.type": "guardrail", "guardrail.name": "pii" }, [
      { name: "guardrail.stream.start", attributes: { "guardrail.stream.handler": true } },
      {
        name: "guardrail.stream.process",
        attributes: {
          "guardrail.chunk.index": 4,
          "guardrail.chunk.type": "text-delta",
          "guardrail.chunk.text": "SECRET_CHUNK_TEXT",
          "guardrail.chunk.action": "pass",
        },
      },
      {
        name: "exception",
        attributes: {
          "exception.type": "Error",
          "exception.message": "guardrail failed",
          "exception.stacktrace": "Error: guardrail failed\n    at handler (guardrail.ts:1:1)",
        },
      },
      {
        name: "provider.request",
        attributes: { "provider.name": "openai", "provider.api_key": "PLACEHOLDER_KEY", "http.request.headers": "PLACEHOLDER_HEADERS" },
      },
      { name: "guardrail.stream.end" },
    ]);
  const workflowSpan = () =>
    fakeSpan("workflow.approval", { "entity.type": "workflow", "entity.id": "approval" }, [
      {
        name: "workflow.suspended",
        attributes: {
          "suspension.step_index": 0,
          "suspension.reason": "needs approval",
          "suspension.data": '{"question":"SECRET_SUSPEND_DATA"}',
          "suspension.checkpoint": '{"stepExecutionState":"SECRET_CHECKPOINT"}',
        },
      },
      { name: "workflow.resumed", attributes: { "resume.step_index": 0, "resume.data": '{"note":"SECRET_RESUME_DATA"}' } },
    ]);

  function exportWith(captureContent: boolean) {
    const exporter = new InMemorySpanExporter();
    const processor = new FIVoltAgentSpanProcessor({ exporter, batch: false, captureContent });
    const originals = [guardrailSpan(), workflowSpan()];
    const before = JSON.stringify(originals.map((span) => span.events));
    for (const span of originals) processor.onEnd(span);
    // The span other processors hold is never modified.
    expect(JSON.stringify(originals.map((span) => span.events))).toBe(before);
    const exported = exporter.getFinishedSpans();
    const event = (spanName: string, eventName: string) => {
      const found = one(exported, spanName).events.filter((e) => e.name === eventName);
      expect(found).toHaveLength(1);
      return found[0].attributes ?? {};
    };
    return { exported, event };
  }

  it("removes content from event attributes by default; names, times, counts and exceptions stay", () => {
    const { exported, event } = exportWith(false);
    expect(one(exported, "guardrail.output.stream.1").events.map((e) => [e.name, e.time])).toEqual([
      ["guardrail.stream.start", [0, 1]],
      ["guardrail.stream.process", [0, 2]],
      ["exception", [0, 3]],
      ["provider.request", [0, 4]],
      ["guardrail.stream.end", [0, 5]],
    ]);
    expect(event("guardrail.output.stream.1", "guardrail.stream.process")).toEqual({
      "guardrail.chunk.index": 4,
      "guardrail.chunk.type": "text-delta",
      "guardrail.chunk.action": "pass",
    });
    expect(event("guardrail.output.stream.1", "exception")).toEqual({
      "exception.type": "Error",
      "exception.message": "guardrail failed",
      "exception.stacktrace": "Error: guardrail failed\n    at handler (guardrail.ts:1:1)",
    });
    expect(event("workflow.approval", "workflow.suspended")).toEqual({
      "suspension.step_index": 0,
      "suspension.reason": "needs approval",
    });
    expect(event("workflow.approval", "workflow.resumed")).toEqual({ "resume.step_index": 0 });
    const blob = JSON.stringify(exported.map((span) => span.events));
    expect(blob).not.toContain("SECRET");
    expect(blob).not.toContain("PLACEHOLDER");
  });

  it("keeps event content after captureContent: true, but never credentials", () => {
    const { exported, event } = exportWith(true);
    expect(event("guardrail.output.stream.1", "guardrail.stream.process")["guardrail.chunk.text"]).toBe(
      "SECRET_CHUNK_TEXT",
    );
    const suspended = event("workflow.approval", "workflow.suspended");
    expect(suspended["suspension.data"]).toBe('{"question":"SECRET_SUSPEND_DATA"}');
    expect(suspended["suspension.checkpoint"]).toBe('{"stepExecutionState":"SECRET_CHECKPOINT"}');
    expect(event("workflow.approval", "workflow.resumed")["resume.data"]).toBe('{"note":"SECRET_RESUME_DATA"}');
    expect(event("guardrail.output.stream.1", "provider.request")).toEqual({ "provider.name": "openai" });
    expect(JSON.stringify(exported.map((span) => span.events))).not.toContain("PLACEHOLDER");
  });
});

describe("span events from a real agent and workflow (M1)", () => {
  async function runEventJourney(captureContent: boolean) {
    const { exporter, provider } = fiProvider();
    const recorder = new RecordingProcessor();
    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider, captureContent });
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

  it("drops streamed guardrail text and workflow suspend/resume payloads from events by default", async () => {
    const { exported, events } = await runEventJourney(false);
    const processed = events("guardrail.stream.process");
    expect(processed.map((a) => a["guardrail.chunk.type"])).toContain("text-delta");
    for (const a of processed) expect(a["guardrail.chunk.text"]).toBeUndefined();
    expect(events("guardrail.stream.start")).toHaveLength(1);
    const [suspended] = events("workflow.suspended");
    expect(suspended).toMatchObject({ "suspension.step_index": 0 });
    expect(suspended["suspension.reason"]).toEqual(expect.any(String));
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

  it("keeps them after captureContent: true (control run)", async () => {
    const { events } = await runEventJourney(true);
    expect(events("guardrail.stream.process").map((a) => a["guardrail.chunk.text"])).toContain(MARKERS.answer);
    const [suspended] = events("workflow.suspended");
    expect(String(suspended["suspension.data"])).toContain(MARKERS.suspendData);
    expect(suspended["suspension.checkpoint"]).toEqual(expect.any(String));
    expect(String(events("workflow.resumed")[0]["resume.data"])).toContain(MARKERS.resumeData);
  });
});

describe("generateObject (README Tokens)", () => {
  it("creates no llm span in 2.11.0: usage is only voltagent.usage.* on the agent span", async () => {
    const { exporter, provider } = fiProvider();
    const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider });
    const recorder = new RecordingProcessor();
    const { model, reported } = scriptedModel("mockai/mock-model-1", [
      { kind: "text", text: '{"city":"Paris"}', input: 9, output: 4 },
    ]);
    const agent = new Agent({ name: "assistant", instructions: "x", model });
    observability = voltagent({ assistant: agent }, [recorder, processor]);
    const result = await agent.generateObject("hi", z.object({ city: z.string() }));
    expect(result.object).toEqual({ city: "Paris" });
    await settle(recorder);
    await processor.forceFlush();
    const spans = exporter.getFinishedSpans();
    expect(reported).toHaveLength(1);
    expect(spans.filter((span) => span.name.startsWith("llm:"))).toHaveLength(0);
    expect(spans.filter((span) => attrs(span)["fi.span.kind"] === "LLM")).toHaveLength(0);
    expect(promotedInputTokens(spans)).toBe(0);
    const root = attrs(one(spans, "assistant"));
    expect(root["voltagent.usage.input_tokens"]).toBe(9);
    expect(root["voltagent.usage.output_tokens"]).toBe(4);
  });
});

describe("usage reconciliation window", () => {
  it("exports a held llm span unreconciled when its root never ends", async () => {
    const exporter = new InMemorySpanExporter();
    const processor = new FIVoltAgentSpanProcessor({ exporter, batch: false, usageReconciliationTimeoutMs: 5 });
    const fakeLLM = {
      name: "llm:generateText",
      kind: SpanKind.CLIENT,
      spanContext: () => ({ traceId: "a".repeat(32), spanId: "b".repeat(16), traceFlags: 1 }),
      parentSpanContext: undefined,
      startTime: [0, 0],
      endTime: [0, 1],
      status: { code: SpanStatusCode.OK },
      attributes: { "span.type": "llm", "operation.id": "op-x", "llm.usage.prompt_tokens": 7 },
      links: [],
      events: [],
      duration: [0, 1],
      ended: true,
      resource: { attributes: {} },
      instrumentationScope: { name: "@voltagent/core" },
      droppedAttributesCount: 0,
      droppedEventsCount: 0,
      droppedLinksCount: 0,
    } as unknown as ReadableSpan;
    processor.onEnd(fakeLLM);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await new Promise((resolve) => setTimeout(resolve, 30));
    const [span] = exporter.getFinishedSpans();
    expect(span.attributes["gen_ai.usage.input_tokens"]).toBe(7);
    expect(span.attributes["voltagent.usage.reconciled"]).toBeUndefined();
  });
});
