/**
 * Typed (number / boolean / numeric array) values written on content keys must be masked under the
 * same hide flags as their string form, by the exported copy of a VoltAgent span.
 */
import { mapSpanEvents, mapVoltAgentAttributes } from "../mapping";
import { SHOW_ALL, type ContentPolicy } from "../privacy";

type Typed = number | boolean | number[];
const TYPED: Array<[string, Typed]> = [
  ["int", 4242],
  ["double", 0.4244],
  ["bool", true],
  ["array", [4243, 4244]],
];

// Input-side content keys (Python TraceConfig input prefixes) and output/either-side ones.
const INPUT_KEYS = ["gen_ai.input.images", "gen_ai.retrieval.query", "gen_ai.reranker.query", "tool_call.function.arguments"];
const EITHER_KEYS = ["retrieval.documents.0.document.content"];

const hiddenPolicy = (flag: keyof ContentPolicy): ContentPolicy => ({ ...SHOW_ALL, [flag]: true });
const toJson = (value: unknown) => JSON.stringify(value);

describe("typed content values on the exported span copy", () => {
  it.each(TYPED)("typedContentValuesAreExportedWithoutHideSettings (%s)", (_name, value) => {
    for (const key of [...INPUT_KEYS, ...EITHER_KEYS]) {
      const { attributes } = mapVoltAgentAttributes({ [key]: value }, { policy: SHOW_ALL });
      expect(attributes[key]).toEqual(value);
    }
  });

  it.each(TYPED)("typedContentValuesAreMaskedUnderHideInputs (%s)", (_name, value) => {
    for (const key of [...INPUT_KEYS, ...EITHER_KEYS]) {
      const { attributes } = mapVoltAgentAttributes({ [key]: value }, { policy: hiddenPolicy("hideInputs") });
      expect({ key, exported: toJson(attributes[key]) }).toEqual({ key, exported: undefined });
    }
  });

  it.each(TYPED)("typedContentValuesAreMaskedUnderHideOutputs (%s)", (_name, value) => {
    for (const key of EITHER_KEYS) {
      const { attributes } = mapVoltAgentAttributes({ [key]: value }, { policy: hiddenPolicy("hideOutputs") });
      expect({ key, exported: toJson(attributes[key]) }).toEqual({ key, exported: undefined });
    }
  });

  it.each(TYPED)("typedEventContentValuesAreMaskedUnderHideInputs (%s)", (_name, value) => {
    for (const key of INPUT_KEYS) {
      const [event] = mapSpanEvents([{ name: "e", attributes: { [key]: value } }], { policy: hiddenPolicy("hideInputs") });
      expect({ key, exported: toJson(event.attributes?.[key]) }).toEqual({ key, exported: undefined });
    }
  });
});
