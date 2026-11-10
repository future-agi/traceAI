import { mapGenkitAttributes, maskAuxiliaryAttributes } from "../mapping";
import { semanticContentSide } from "../content-policy";
import { SHOW_ALL } from "../privacy";

const marker = "SYNTHETIC QUERY CONTENT";
const capsule = "traceai.attribute_projection.metadata";

describe("composed semantic policy", () => {
  it("classifies vector query predicates as inputs, including flattened descendants", () => {
    for (const key of ["db.vector.query.filter", "db.vector.query.filter.0.content", "db.vector.query.filter[0].content"]) {
      expect(semanticContentSide(key)).toBe("input");
      for (const flag of ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"]) {
        expect(maskAuxiliaryAttributes({ [key]: marker }, { ...SHOW_ALL, [flag]: true })[key]).toBe("__REDACTED__");
      }
      expect(maskAuxiliaryAttributes({ [key]: marker }, { ...SHOW_ALL, hideOutputs: true })[key]).toBe(marker);
      expect(maskAuxiliaryAttributes({ [key]: marker }, SHOW_ALL)[key]).toBe(marker);
    }
  });

  it("re-enters the same policy for nested legacy attribute maps without changing the source", () => {
    const attributes = { [capsule]: JSON.stringify({
      "gen_ai.prompts": marker, "gen_ai.request.model": "public-model",
      [capsule]: JSON.stringify({ "gen_ai.agent.input": marker, "gen_ai.future_payload": marker }),
    }) };
    const original = JSON.stringify(attributes);
    expect(maskAuxiliaryAttributes(attributes, SHOW_ALL)).toEqual(attributes);
    for (const result of [
      mapGenkitAttributes(attributes, { policy: { ...SHOW_ALL, hideInputs: true } }),
      maskAuxiliaryAttributes(attributes, { ...SHOW_ALL, hideInputs: true }),
    ]) {
      const outer = JSON.parse(String(result[capsule]));
      expect(outer["gen_ai.prompts"]).toBe("__REDACTED__");
      expect(outer["gen_ai.request.model"]).toBe("public-model");
      expect(JSON.parse(outer[capsule])).toEqual({ "gen_ai.agent.input": "__REDACTED__", "gen_ai.future_payload": "__REDACTED__" });
    }
    expect(JSON.stringify(attributes)).toBe(original);
  });

  it("does not use a legacy capsule to restore native context attributes", () => {
    const attributes = { [capsule]: JSON.stringify({
      "genkit:metadata:context": marker,
      [capsule]: JSON.stringify({ "genkit:metadata:context": marker }),
    }) };
    for (const policy of [SHOW_ALL, { ...SHOW_ALL, hideInputs: true }]) {
      const out = maskAuxiliaryAttributes(attributes, policy);
      expect(JSON.stringify(out)).not.toContain(marker);
      const outer = JSON.parse(String(out[capsule]));
      expect(outer["genkit:metadata:context"]).toBeUndefined();
      expect(JSON.parse(outer[capsule])["genkit:metadata:context"]).toBeUndefined();
    }
  });

  it("fails closed on malformed or excessively nested capsules", () => {
    expect(maskAuxiliaryAttributes({ [capsule]: "not JSON" }, { ...SHOW_ALL, hideInputs: true })[capsule]).toBe("__REDACTED__");
    let value = JSON.stringify({ "gen_ai.prompts": marker });
    for (let i = 0; i < 17; i++) value = JSON.stringify({ [capsule]: value });
    expect(JSON.stringify(maskAuxiliaryAttributes({ [capsule]: value }, { ...SHOW_ALL, hideInputs: true }))).not.toContain(marker);
  });
});
