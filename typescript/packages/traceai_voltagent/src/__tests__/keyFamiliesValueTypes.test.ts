import { mapVoltAgentAttributes } from "../mapping";
import { SHOW_ALL } from "../privacy";

const HIDE = { hideInputs: false, hideOutputs: false, hideInputMessages: false, hideOutputMessages: false };
const withFlag = (flag: string) => ({ policy: { ...SHOW_ALL, ...HIDE, [flag]: true } });
const ALL = { policy: { ...SHOW_ALL, ...HIDE, hideInputs: true, hideOutputs: true, hideInputMessages: true, hideOutputMessages: true } };
const NONE = { policy: SHOW_ALL };

function exported(key: string, options: Parameters<typeof mapVoltAgentAttributes>[1], value: string | number | boolean): boolean {
  return key in mapVoltAgentAttributes({ [key]: value }, options).attributes;
}

describe("a content family member is content whatever its value type", () => {
  const INPUT = ["gen_ai.tool.call.arguments.amount", "tool.parameters.limit", "suspension.data.balance", "eval.expected.0", "input.value.total"];
  const OUTPUT = ["gen_ai.tool.call.result.total", "planagent.todos.0.done", "eval.expected.1"];
  const MESSAGE_IN = ["llm.messages.0.tokens", "agent.messages.0.parts.0.index"];

  it.each([...INPUT, "suspension.data.balance"].flatMap((k) => [[k, 1500], [k, true]] as const))("input family %s (%s) hides under hideInputs", (key, value) => {
    expect(exported(key, withFlag("hideInputs"), value)).toBe(false);
    expect(exported(key, NONE, value)).toBe(true);
  });
  it.each(OUTPUT.flatMap((k) => [[k, 42], [k, false]] as const))("output family %s (%s) hides under hideOutputs", (key, value) => {
    expect(exported(key, withFlag("hideOutputs"), value)).toBe(false);
    expect(exported(key, NONE, value)).toBe(true);
  });
  it.each(MESSAGE_IN)("input message child %s hides under hideInputMessages", (key) => {
    expect(exported(key, withFlag("hideInputMessages"), 7)).toBe(false);
    expect(exported(key, NONE, 7)).toBe(true);
  });
  it.each(["suspension.data.balance", "eval.expected.0"])("both-side %s hides under either flag", (key) => {
    expect(exported(key, withFlag("hideInputs"), 900)).toBe(false);
    expect(exported(key, withFlag("hideOutputs"), 900)).toBe(false);
  });
});

/** The count keys VoltAgent itself writes: visible under every hide flag. */
const COUNT_KEYS = [
  "llm.messages.count", "llm.tools.count", "eval.messages.count", "eval.scorers.count", "eval.tool_calls.count",
  "eval.tool_results.count", "memory.step.count", "tool.search.selection.count", "workflow.step.retry.count", "agent.summary.length",
];

describe("counts stay visible only by allowlist", () => {
  it.each(COUNT_KEYS)("numeric %s under every flag", (key) => {
    expect(exported(key, ALL, 3)).toBe(true);
  });
  it("a non-numeric value under a count key is still content", () => {
    expect(exported("llm.messages.count", ALL, "SYNTH")).toBe(false);
  });
  it.each([
    ["tool.parameters.count", "hideInputs"],
    ["gen_ai.tool.call.arguments.size", "hideInputs"],
    ["gen_ai.tool.call.result.length", "hideOutputs"],
    ["suspension.data.count", "hideInputs"],
    ["suspension.data.count", "hideOutputs"],
    ["eval.expected.length", "hideInputs"],
    ["eval.expected.length", "hideOutputs"],
    ["gen_ai.tool.definitions.length", "hideInputs"],
    ["gen_ai.prompts.count", "hideInputs"],
    ["agent.messages.size", "hideInputMessages"],
  ])("numeric %s (a count-like name inside a content family) hides under %s", (key, flag) => {
    expect(exported(key, withFlag(flag), 3)).toBe(false);
    expect(exported(key, NONE, 3)).toBe(true);
  });
});

describe("message flags apply to every child of a message family", () => {
  it.each(["llm.messages.0.message.content", "agent.messages.0.parts.0.content", "llm.messages.0.message.role", "agent.messages.ui.0.data"])(
    "%s hides under hideInputMessages",
    (key) => {
      expect(exported(key, withFlag("hideInputMessages"), "SYNTH")).toBe(false);
      expect(exported(key, withFlag("hideInputs"), "SYNTH")).toBe(false);
      expect(exported(key, withFlag("hideOutputMessages"), "SYNTH")).toBe(true);
      expect(exported(key, NONE, "SYNTH")).toBe(true);
    },
  );
  it.each(["gen_ai.output.messages.0.content", "gen_ai.output.messages.0.parts.0.data", "gen_ai.output.messages.0.role"])(
    "%s hides under hideOutputMessages",
    (key) => {
      expect(exported(key, withFlag("hideOutputMessages"), "SYNTH")).toBe(false);
      expect(exported(key, withFlag("hideOutputs"), "SYNTH")).toBe(false);
      expect(exported(key, withFlag("hideInputMessages"), "SYNTH")).toBe(true);
      expect(exported(key, NONE, "SYNTH")).toBe(true);
    },
  );
  it.each(["tool.name", "tool.call.id", "gen_ai.tool.name", "gen_ai.tool.call.id"])("%s stays visible under every flag", (key) => {
    expect(exported(key, ALL, "SYNTH")).toBe(true);
  });
});
