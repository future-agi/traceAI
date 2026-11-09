import { mapGenkitAttributes, maskAuxiliaryAttributes } from "../mapping";
import { exportEvents } from "../processor";
import { SHOW_ALL } from "../privacy";

const marker = "SYNTHETIC_SURFACE_PRIVACY_MARKER";

describe("content policy on auxiliary attribute maps", () => {
  for (const side of ["input", "output"] as const) {
    const flag = side === "input" ? "hideInputs" : "hideOutputs";
    const native = side === "input" ? ["genkit:input", "genkit:init", "genkit:metadata:resumed"] : ["genkit:output", "genkit:metadata:interrupt"];
    const attrs = Object.fromEntries([...native, `${side}.value`, `raw.${side}`, `gen_ai.${side}.messages.0.content.0.text`].map(key => [key, marker]));
    it(`${side} context aliases cannot repopulate a hidden native value`, () => {
      const source = { "genkit:type": "flow", [`genkit:${side}`]: "public native" };
      const result = mapGenkitAttributes(source, { policy: { ...SHOW_ALL, [flag]: true }, contextAttributes: attrs });
      expect(JSON.stringify(result)).not.toContain(marker);
      expect(result[`${side}.value`]).toBe("__REDACTED__");
      expect(result[`genkit:${side}`]).toBeUndefined();
      expect(attrs[`${side}.value`]).toBe(marker);
      expect(source[`genkit:${side}`]).toBe("public native");
    });
    it(`${side} auxiliary attributes retain positive controls`, () => {
      expect(maskAuxiliaryAttributes(attrs, SHOW_ALL)).toEqual(attrs);
      expect(maskAuxiliaryAttributes(attrs, { ...SHOW_ALL, [flag]: true })).toEqual({
        [`${side}.value`]: "__REDACTED__", [`raw.${side}`]: "__REDACTED__",
        [`gen_ai.${side}.messages.0.content.0.text`]: "__REDACTED__",
      });
    });
    it(`${side} events use the content policy without changing native objects`, () => {
      const events = [{ name: "synthetic-content", time: [1, 0] as [number, number], attributes: attrs }];
      const before = JSON.stringify(events);
      const result = exportEvents(events, { policy: { ...SHOW_ALL, [flag]: true } });
      expect(JSON.stringify(result)).not.toContain(marker);
      expect(JSON.stringify(events)).toBe(before);
      expect(exportEvents(events, { policy: SHOW_ALL })).toEqual(events);
    });
  }
});
