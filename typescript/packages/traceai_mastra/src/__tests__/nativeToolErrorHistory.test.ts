import type { AnyExportedSpan } from "@mastra/core/observability";
import { NativeToolErrorHistory } from "../toolErrorHistory";

const secret = "PRIVATE_TERMINAL_TOOL_ERROR";
const native = (traceId: string, id: string, type: string, extra = {}): AnyExportedSpan =>
  ({ traceId, id, type, ...extra }) as AnyExportedSpan;
const root = (trace = "first", agent = "agent-a") => native(trace, "root", "agent_run", { entityId: agent });
const step = (trace = "first") => native(trace, "step", "model_step", { parentSpanId: "root",
  output: { toolCalls: [
    { toolCallId: "failed-call", toolName: "lookup", args: { a: 1, b: 2 } },
    { toolCallId: "safe-call", toolName: "lookup", args: { a: 2, b: 2 } },
  ] } });
const tool = (direct: boolean) => native("first", "tool", "tool_call", {
  entityId: "lookup", parentSpanId: "step", input: { b: 2, a: 1 },
  errorInfo: { message: secret }, attributes: direct ? { toolCallId: "failed-call" } : {},
});

for (const direct of [false, true]) {
  for (const outcome of ["success", "error", "abort"]) {
    for (const order of ["tool-first", "step-first"]) {
      it(`captures terminal ${outcome}: direct ID ${direct}, ${order}, without another request`, () => {
        const history = new NativeToolErrorHistory();
        const snapshots = [root(), step(), tool(direct)];
        const before = JSON.stringify(snapshots);
        history.capture(root(), false);
        history.capture(step(), false);
        for (const span of order === "tool-first" ? [tool(direct), step()] : [step(), tool(direct)]) {
          history.capture(span, true);
        }
        history.capture(native("first", "root", "agent_run", { entityId: "agent-a", outcome }), true);
        history.forget("first");
        history.capture(root("later"), false);
        history.capture(step("later"), false);
        expect(history.matches(step("later"), secret, "failed-call")).toBe(true);
        expect(history.matches(step("later"), secret, "safe-call")).toBe(false);
        expect(history.matches(step("later"), "safe result", "failed-call")).toBe(false);
        expect(history.matches(step("later"), secret)).toBe(false);
        expect(JSON.stringify(snapshots)).toBe(before);
        const state = JSON.stringify(history, (_, value) => value instanceof Map || value instanceof Set ? [...value] : value);
        expect(state).not.toContain(secret);
        expect(state).not.toContain("failed-call");
        expect(state).not.toContain("lookup");
        history.capture(root("other", "agent-b"), false);
        history.capture(step("other"), false);
        expect(history.matches(step("other"), secret, "failed-call")).toBe(false);
        expect(new NativeToolErrorHistory().matches(step("later"), secret, "failed-call")).toBe(false);
        history.clear();
        expect(history.matches(step("later"), secret, "failed-call")).toBe(false);
      });
    }
  }
}

it("captures the native call ID even when error/cancel omits step completion", () => {
  const history = new NativeToolErrorHistory();
  history.capture(root(), false);
  history.capture(step(), false);
  history.capture(tool(true), true);
  history.forget("first");
  history.capture(root("later"), false);
  expect(history.matches(root("later"), secret, "failed-call")).toBe(true);
});

it("scopes nested agents to their nearest native agent, not the outer trace owner", () => {
  const history = new NativeToolErrorHistory();
  history.capture(root(), false);
  history.capture(native("first", "nested", "agent_run", { entityId: "agent-b", parentSpanId: "root" }), false);
  history.capture(native("first", "step", "model_step", { parentSpanId: "nested" }), false);
  history.capture(tool(true), true);
  history.forget("first");
  history.capture(root("later", "agent-a"), false);
  expect(history.matches(root("later"), secret, "failed-call")).toBe(false);
  history.capture(root("nested-later", "agent-b"), false);
  expect(history.matches(root("nested-later"), secret, "failed-call")).toBe(true);
});

it("bounds native joins and fails closed on saturation only in the affected agent", () => {
  const history = new NativeToolErrorHistory();
  history.capture(root(), false);
  for (let i = 0; i < 130; i++) history.capture(native("first", `step-${i}`, "model_step", {
    parentSpanId: "root", output: { toolCalls: [{ toolCallId: `call-${i}`, toolName: "lookup", args: i }] },
  }), true);
  history.forget("first");
  history.capture(root("later"), false);
  expect(history.matches(root("later"), secret, "untracked-call")).toBe(true);
  history.capture(root("other", "agent-b"), false);
  expect(history.matches(root("other"), secret, "untracked-call")).toBe(false);
});
