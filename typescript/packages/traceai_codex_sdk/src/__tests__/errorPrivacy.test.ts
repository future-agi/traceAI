import { SpanStatusCode } from "@opentelemetry/api";
import { CodexSDKInstrumentation } from "../index";
import { resolveContentPolicy } from "../instrumentation";
import { ThreadEventLike } from "../types";
import { fakeModule, scripted } from "./fixtures/fakeThread";
import { drain, memoryProvider, one } from "./helpers";

const MARKER = "ERROR_DETAIL_MUST_NOT_LEAK";
const envBefore = process.env.FI_HIDE_ERROR_DETAILS;
afterEach(() => {
  if (envBefore === undefined) delete process.env.FI_HIDE_ERROR_DETAILS;
  else process.env.FI_HIDE_ERROR_DETAILS = envBefore;
});
beforeEach(() => { delete process.env.FI_HIDE_ERROR_DETAILS; });

it.each([undefined, true])("ordinary output opt-in does not reveal error details (%s)", async (hideErrorDetails) => {
  const { provider, exporter } = memoryProvider();
  const module = fakeModule();
  new CodexSDKInstrumentation({ tracerProvider: provider, traceConfig: { hideOutputs: false, hideErrorDetails } }).manuallyInstrument(module);
  const events: ThreadEventLike[] = [
    { type: "thread.started", thread_id: "privacy-thread" },
    { type: "turn.started" },
    { type: "error", message: MARKER },
    { type: "item.completed", item: { id: "warning", type: "error", message: MARKER } },
    { type: "item.completed", item: { id: "cmd", type: "command_execution", status: "failed", exit_code: 1, aggregated_output: MARKER } },
    { type: "item.completed", item: { id: "mcp", type: "mcp_tool_call", status: "failed", tool: "read", error: { message: MARKER }, result: { content: MARKER } } },
    { type: "turn.failed", error: { message: MARKER } },
  ];
  const thread = new module.Codex([scripted(events)]).startThread();
  await drain((await thread.runStreamed("safe prompt")).events);
  const spans = exporter.getFinishedSpans();
  expect(one(spans, "codex.turn").status).toEqual({ code: SpanStatusCode.ERROR });
  expect(one(spans, "codex.command_execution").attributes["codex.command.exit_code"]).toBe(1);
  expect(one(spans, "codex.error").attributes["codex.warning"]).toBe(true);
  expect(JSON.stringify(spans.map((s) => [s.attributes, s.status, s.events]))).not.toContain(MARKER);
});

it("does not leak a thrown error through the custom attribute when outputs alone are enabled", async () => {
  const { provider, exporter } = memoryProvider();
  const module = fakeModule();
  new CodexSDKInstrumentation({ tracerProvider: provider, traceConfig: { hideOutputs: false } }).manuallyInstrument(module);
  const error = new Error(MARKER);
  const thread = new module.Codex([scripted([{ type: "thread.started", thread_id: "privacy-thread" }], { throwAfter: error })]).startThread();
  await expect(thread.run("safe prompt")).rejects.toBe(error);
  const span = one(exporter.getFinishedSpans(), "codex.turn");
  expect(span.attributes["codex.error.type"]).toBe("Error");
  expect(span.attributes["codex.error.message"]).toBeUndefined();
  expect(JSON.stringify([span.attributes, span.status, span.events])).not.toContain(MARKER);
});

it("retains the fixed summary only when error details are enabled but outputs remain hidden", async () => {
  const { provider, exporter } = memoryProvider();
  const module = fakeModule();
  new CodexSDKInstrumentation({ tracerProvider: provider, traceConfig: { hideOutputs: true, hideErrorDetails: false } }).manuallyInstrument(module);
  const error = new Error(`Codex Exec exited with code 1: ${MARKER}`);
  const thread = new module.Codex([scripted([{ type: "thread.started", thread_id: "privacy-thread" }], { throwAfter: error })]).startThread();
  await expect(thread.run("safe prompt")).rejects.toBe(error);
  const span = one(exporter.getFinishedSpans(), "codex.turn");
  expect(span.status.message).toBe("Codex Exec exited with code 1");
  expect(span.attributes["codex.error.message"]).toBe("Codex Exec exited with code 1");
  expect(JSON.stringify([span.attributes, span.status, span.events])).not.toContain(MARKER);
});

it.each([undefined, "", "0", "off", "true", "invalid"])("error privacy fails closed for env %s", (value) => {
  if (value !== undefined) process.env.FI_HIDE_ERROR_DETAILS = value;
  expect(resolveContentPolicy().hideErrorDetails).toBe(true);
});

it("explicit error privacy options override the environment", () => {
  process.env.FI_HIDE_ERROR_DETAILS = " FALSE ";
  expect(resolveContentPolicy().hideErrorDetails).toBe(false);
  expect(resolveContentPolicy({ hideErrorDetails: true }).hideErrorDetails).toBe(true);
  process.env.FI_HIDE_ERROR_DETAILS = "true";
  expect(resolveContentPolicy({ hideErrorDetails: false }).hideErrorDetails).toBe(false);
});
