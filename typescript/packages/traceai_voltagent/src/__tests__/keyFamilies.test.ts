/**
 * Content keys are families: `<key>` and `<key>.<anything>` take the same side and the same hide
 * flags as the exact key (fi-core maskingRules matches `key === prefix || key.startsWith(prefix + ".")`).
 */
import { mapVoltAgentAttributes } from "../mapping";
import { SHOW_ALL } from "../privacy";

const INPUT_KEYS = [
  "input",
  "agent.instructions",
  "agent.context",
  "workflow.context",
  "workspace.sandbox.command",
  "workspace.sandbox.args",
  "workspace.fs.path",
  "workspace.fs.pattern",
  "workspace.sandbox.cwd",
  "planagent.task.description",
  "resume.data",
  "input.value",
  "gen_ai.prompts",
  "gen_ai.system_instructions",
  "tool.description",
  "gen_ai.tool.description",
  "tool.parameters",
  "gen_ai.tool.parameters",
  "tool.json_schema",
  "gen_ai.tool.definitions",
  "gen_ai.tool.call.arguments",
  "gen_ai.request.parameters",
  "gen_ai.agent.description",
];
const INPUT_MESSAGE_KEYS = ["llm.messages", "agent.messages", "agent.messages.ui", "gen_ai.input.messages"];
const OUTPUT_KEYS = [
  "output",
  "output.value",
  "agent.summary.preview",
  "agent.summary.text",
  "planagent.todos",
  "planagent.task.response_preview",
  "guardrail.chunk.text",
  "gen_ai.tool.call.result",
];
const OUTPUT_MESSAGE_KEYS = ["gen_ai.output.messages"];
const BOTH_KEYS = [
  "agent.stateSnapshot",
  "workflow.stateSnapshot",
  "suspension.checkpoint",
  "suspension.data",
  "agent.workingMemory.finalContent",
  "eval.scorer.metadata",
  "eval.expected",
  "guardrail.result.metadata",
];

const HIDE = { hideInputs: false, hideOutputs: false, hideInputMessages: false, hideOutputMessages: false };
const withFlag = (flag: string) => ({ policy: { ...SHOW_ALL, ...HIDE, [flag]: true } });
const NONE = { policy: SHOW_ALL };

/** An indexed child and a dotted child whose last segment is not itself a content segment. */
const children = (key: string) => [`${key}.0.zz_field`, `${key}.zz_group.zz_field`];

function exported(key: string, options: Parameters<typeof mapVoltAgentAttributes>[1]): boolean {
  return key in mapVoltAgentAttributes({ [key]: "SYNTH_VALUE" }, options).attributes;
}

describe("content key families", () => {
  describe.each([
    ["input", INPUT_KEYS, "hideInputs", "hideOutputs"],
    ["input message", INPUT_MESSAGE_KEYS, "hideInputMessages", "hideOutputs"],
    ["output", OUTPUT_KEYS, "hideOutputs", "hideInputs"],
    ["output message", OUTPUT_MESSAGE_KEYS, "hideOutputMessages", "hideInputs"],
  ])("%s keys", (_name, keys, hiding, other) => {
    it.each(keys.flatMap((k) => [k, ...children(k)]))("%s follows its flag", (key) => {
      expect(exported(key, withFlag(hiding))).toBe(false);
      expect(exported(key, withFlag(other))).toBe(true);
      expect(exported(key, NONE)).toBe(true);
    });
  });

  describe("input and output message keys also hide under the whole-side flag", () => {
    it.each([...INPUT_MESSAGE_KEYS.flatMap((k) => children(k)), ...OUTPUT_MESSAGE_KEYS.flatMap((k) => children(k))])(
      "%s",
      (key) => {
        const flag = OUTPUT_MESSAGE_KEYS.some((k) => key.startsWith(`${k}.`)) ? "hideOutputs" : "hideInputs";
        expect(exported(key, withFlag(flag))).toBe(false);
      },
    );
  });

  it.each(BOTH_KEYS.flatMap((k) => [k, ...children(k)]))("%s is hidden by either flag", (key) => {
    expect(exported(key, withFlag("hideInputs"))).toBe(false);
    expect(exported(key, withFlag("hideOutputs"))).toBe(false);
    expect(exported(key, NONE)).toBe(true);
  });

  it.each(["tool.name", "tool.call.id", "gen_ai.tool.name", "gen_ai.tool.call.id", "tool.count", "gen_ai.tool.definitions.count"])(
    "%s stays visible under every hide flag",
    (key) => {
      const all = { policy: { ...SHOW_ALL, hideInputs: true, hideOutputs: true, hideInputMessages: true, hideOutputMessages: true } };
      expect(exported(key, all)).toBe(true);
    },
  );
});
