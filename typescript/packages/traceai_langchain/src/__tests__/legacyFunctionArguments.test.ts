import { describe, expect, it } from "@jest/globals";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import {
  safelyFormatFunctionCalls,
  safelyFormatInputMessages,
  safelyFormatOutputMessages,
} from "../utils";

// Exercise the serializer before FI privacy admission. The real-adapter
// contract/privacy-copies.mjs separately requires credential redaction and
// removal of every argument copy under output hiding.
const canonicalArguments = JSON.stringify({
  query: "synthetic-private-legacy-function-query",
  secret: "synthetic-private-legacy-function-argument",
});
const fallbackArguments = JSON.stringify({ query: "synthetic-fallback-query" });

function messageWithCall(functionCall: Record<string, unknown>) {
  return {
    lc_id: ["langchain_core", "messages", "AIMessage"],
    lc_kwargs: { content: "", additional_kwargs: { function_call: functionCall } },
    additional_kwargs: { function_call: functionCall },
  };
}

const cases: {
  name: string;
  fields: Record<string, unknown>;
  expected: string | undefined;
}[] = [
  { name: "canonical arguments", fields: { arguments: canonicalArguments }, expected: canonicalArguments },
  { name: "legacy args fallback", fields: { args: fallbackArguments }, expected: fallbackArguments },
  { name: "canonical precedence", fields: { arguments: canonicalArguments, args: fallbackArguments }, expected: canonicalArguments },
  { name: "empty canonical string", fields: { arguments: "", args: fallbackArguments }, expected: "" },
  { name: "non-string canonical fallback", fields: { arguments: 7, args: fallbackArguments }, expected: fallbackArguments },
  { name: "missing arguments", fields: {}, expected: undefined },
  { name: "non-string arguments", fields: { arguments: { query: "not-a-string" }, args: 7 }, expected: undefined },
];

describe("legacy function argument serialization", () => {
  for (const direction of ["input", "output"] as const) {
    for (const { name, fields, expected } of cases) {
      it(`${direction} messages preserve ${name} without mutating the message`, () => {
        const functionCall = Object.freeze({ name: "lookup", ...fields });
        const message = messageWithCall(functionCall);
        const before = JSON.stringify(message);
        const attributes = direction === "input"
          ? safelyFormatInputMessages({ messages: [[message]] })
          : safelyFormatOutputMessages({ generations: [[{ message }]] });
        const key = direction === "input" ? SC.LLM_INPUT_MESSAGES : SC.LLM_OUTPUT_MESSAGES;
        const serialized = attributes?.[key];
        expect(typeof serialized).toBe("string");
        if (typeof serialized !== "string") throw new Error("Missing serialized messages");
        const expectedCall = expected === undefined
          ? { name: "lookup" }
          : { name: "lookup", arguments: expected };
        expect(JSON.parse(serialized)).toEqual([
          { role: "assistant", function_call: expectedCall },
        ]);
        expect(JSON.stringify(message)).toBe(before);
      });
    }
  }

  it("keeps canonical arguments in both ordinary and opaque copies before privacy admission", () => {
    const functionCall = Object.freeze({ name: "lookup", arguments: canonicalArguments });
    const message = messageWithCall(functionCall);
    const outputs = { generations: [[{ message }]] };
    const ordinary = safelyFormatOutputMessages(outputs)?.[SC.LLM_OUTPUT_MESSAGES];
    const opaque = safelyFormatFunctionCalls(outputs)?.[SC.LLM_FUNCTION_CALL];
    expect(typeof ordinary).toBe("string");
    expect(typeof opaque).toBe("string");
    if (typeof ordinary !== "string" || typeof opaque !== "string") {
      throw new Error("Missing legacy function copies");
    }
    const messages = JSON.parse(ordinary);
    expect(messages).toHaveLength(1);
    expect(messages[0].function_call).toEqual(functionCall);
    expect(JSON.parse(opaque)).toEqual(functionCall);
    expect(JSON.parse(messages[0].function_call.arguments)).toEqual({
      query: "synthetic-private-legacy-function-query",
      secret: "synthetic-private-legacy-function-argument",
    });
  });
});
