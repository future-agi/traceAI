import { safelyGetFIAttributes } from "../utils";

function check(condition: boolean, id: string): asserts condition {
  if (!condition) throw new Error(id);
}

function checkBothPaths(fields: Record<string, unknown>, expectedArguments: string) {
  const call = { type: "tool-call", toolCallId: "call-a", toolName: "sample", ...fields };
  const routes = [
    {
      source: "ai.prompt.messages",
      value: [{ role: "assistant", content: [call] }],
      target: "gen_ai.input.messages",
    },
    {
      source: "ai.response.toolCalls",
      value: [call],
      target: "gen_ai.output.messages",
    },
  ];

  for (const route of routes) {
    const attributes = safelyGetFIAttributes({
      "operation.name": "ai.generateText.doGenerate sample",
      [route.source]: JSON.stringify(route.value),
    });
    const encoded = attributes?.[route.target];
    check(typeof encoded === "string", "B2-VTC-H01");
    let messages: unknown;
    try {
      messages = JSON.parse(encoded);
    } catch {
      throw new Error("B2-VTC-H02");
    }
    check(Array.isArray(messages) && messages.length === 1, "B2-VTC-H03");
    const message = messages[0];
    check(message !== null && typeof message === "object", "B2-VTC-H04");
    check(message.role === "assistant", "B2-VTC-H05");
    const calls = message.tool_calls;
    check(Array.isArray(calls) && calls.length === 1, "B2-VTC-H06");
    const actual = calls[0];
    check(actual !== null && typeof actual === "object", "B2-VTC-H07");
    check(actual.id === "call-a", "B2-VTC-H08");
    check(actual.function !== null && typeof actual.function === "object", "B2-VTC-H09");
    check(actual.function.name === "sample", "B2-VTC-H10");
    check(actual.function.arguments === expectedArguments, "B2-VTC-H11");
  }
}

test("B2-VTC-001", () => {
  checkBothPaths({ input: { count: 2 }, args: { count: 1 } }, '{"count":2}');
});

test("B2-VTC-002", () => {
  checkBothPaths({ args: { count: 1 } }, '{"count":1}');
});

test("B2-VTC-003", () => {
  checkBothPaths({ input: null, args: { count: 1 } }, '{"count":1}');
});

test("B2-VTC-004", () => {
  checkBothPaths({ input: '{"count":2}', args: '{"count":1}' }, '{"count":2}');
  checkBothPaths({ args: '{"count":1}' }, '{"count":1}');
});

test("B2-VTC-005", () => {
  checkBothPaths({ input: false, args: true }, "false");
  checkBothPaths({ input: 0, args: 1 }, "0");
  checkBothPaths({ input: "", args: "sample" }, "");
});

test("B2-VTC-006", () => {
  checkBothPaths({ args: [0, false] }, "[0,false]");
});
