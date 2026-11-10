import { maskAuxiliaryAttributes, mapGenkitAttributes } from "../mapping";
import { SHOW_ALL, type ContentPolicy } from "../privacy";

const marker = "SYNTHETIC_SEMANTIC_ALIAS_MARKER";
const families: [string, (keyof ContentPolicy)[]][] = [
  ["gen_ai.prompt", ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"]],
  ["gen_ai.system_instructions", ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"]],
  ["gen_ai.completion", ["hideOutputs", "hideOutputMessages", "hideOutputText"]],
];

for (const [alias, flags] of families) {
  for (const key of [alias, `${alias}.0.text`]) {
    for (const flag of flags) {
      it(`${key} follows ${flag} on native, context and auxiliary maps`, () => {
        const original = { [key]: marker, "custom.safe": "retained" };
        for (const hidden of [false, true]) {
          const policy = { ...SHOW_ALL, [flag]: hidden };
          for (const actual of [
            mapGenkitAttributes(original, { policy }),
            mapGenkitAttributes({}, { policy, contextAttributes: original }),
            maskAuxiliaryAttributes(original, policy),
          ]) {
            expect(actual[key]).toBe(hidden ? "__REDACTED__" : marker);
            expect(actual["custom.safe"]).toBe("retained");
          }
        }
        expect(original[key]).toBe(marker);
      });
    }
    it(`${key} is not hidden by the other content side`, () => {
      const policy = { ...SHOW_ALL, [alias === "gen_ai.completion" ? "hideInputs" : "hideOutputs"]: true };
      expect(maskAuxiliaryAttributes({ [key]: marker }, policy)[key]).toBe(marker);
    });
  }
}
