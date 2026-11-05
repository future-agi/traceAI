import { deltaEvent, stepEvent } from "../events";

const toolTypes = ["shell", "write", "delete", "glob", "grep", "read", "edit", "ls", "readLints",
  "mcp", "generateImage", "recordScreen", "computerUse", "semSearch", "createPlan", "updateTodos", "task"];

it.each(toolTypes)("normalizes the known %s protobuf tool without mutating the snapshot", type => {
  const args = Object.freeze({ path: "input" });
  const value = Object.freeze({ text: "output" });
  const result = Object.freeze({ success: value });
  const body = Object.freeze({ args, result });
  const proto = Object.freeze({ toolCall: Object.freeze({ callId: "call", [`${type}ToolCall`]: body }) });
  const normalized = { type: "toolCall", message: { type, callId: "call", args, result: { status: "success", value } } };
  const event = stepEvent(proto);
  expect(event).toEqual(stepEvent(normalized));
  expect(event?.status).toBe("completed");
  expect(event?.args).toBe(args);
  expect(event?.result).toEqual({ status: "success", value });
  expect(body.result).toBe(result);
  expect(result).toEqual({ success: value });
});

it.each(["normalized", "protobuf"])("normalizes %s error results before assigning tool status", shape => {
  const error = Object.freeze({ message: "private-error" });
  const result = Object.freeze({ error });
  const body = { args: {}, result, callId: "inner-id" };
  const value = shape === "normalized" ? { type: "toolCall", message: { type: "read", ...body } }
    : { toolCall: { readToolCall: body } };
  expect(stepEvent(value)).toMatchObject({ name: "read", call_id: "inner-id", status: "error", result: { status: "error", error } });
  expect(result).toEqual({ error });
});

it("normalizes result oneofs in delta callbacks while keeping starts running", () => {
  const toolCall = { type: "read", args: {}, result: { error: { message: "private-error" } } };
  expect(deltaEvent({ type: "tool-call-started", callId: "id", toolCall })?.status).toBe("running");
  expect(deltaEvent({ type: "tool-call-completed", callId: "id", toolCall })).toMatchObject({
    call_id: "id", status: "error", result: { status: "error", error: { message: "private-error" } },
  });
});

it.each(["assistantMessage", "thinkingMessage"])("normalizes protobuf %s", type => {
  const message = { text: "output", thinkingDurationMs: 12 };
  expect(stepEvent({ [type]: message })).toEqual(stepEvent({ type, message }));
  expect(stepEvent({ [type]: message })).toBeDefined();
});

it.each([null, [], {}, { toolCall: {} }, { toolCall: { unknownToolCall: { args: {}, result: { success: {} } } } },
  { toolCall: { shellToolCall: "invalid" } }, { assistantMessage: { text: 123 } }, { userMessage: { text: "input" } },
  { type: "unknown", message: { text: "unknown" } }])("ignores unsupported or malformed snapshots: %j", value => {
  expect(stepEvent(value)).toBeUndefined();
});

it("prefers an error when a malformed result includes both oneof variants", () => {
  expect(stepEvent({ toolCall: { readToolCall: { result: { success: {}, error: "failed" } } } })).toMatchObject({
    status: "error", result: { status: "error", error: "failed" },
  });
});
