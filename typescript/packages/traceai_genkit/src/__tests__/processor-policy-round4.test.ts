import { maskAuxiliaryAttributes, mapGenkitAttributes } from "../mapping";
import { toExportableSpan } from "../processor";
import { SHOW_ALL, type ContentPolicy } from "../privacy";
import { v1Span } from "./fixtures";

const marker = "SYNTHETIC_PROCESSOR_POLICY_MARKER";
const families: [string, (keyof ContentPolicy)[]][] = [
  ["gen_ai.tool.call.arguments", ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"]],
  ["gen_ai.tool.description", ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"]],
  ["gen_ai.request.stop_sequences", ["hideInputs", "hideInputMessages", "hideInputText", "hideInputImages"]],
  ["gen_ai.tool.call.result", ["hideOutputs", "hideOutputMessages", "hideOutputText"]],
];
const span = () => v1Span({ name: "public-flow", attributes: {}, spanId: "1111111111111111" });
for (const [alias, flags] of families) {
  for (const key of [alias, `${alias}.0.text`]) {
    for (const flag of flags) {
      it(`${key} follows ${flag} on every attribute surface without mutation`, () => {
        const original = { [key]: marker, "service.name": "public-service", "gen_ai.tool.name": "public-tool" };
        for (const hidden of [false, true]) {
          const policy = { ...SHOW_ALL, [flag]: hidden };
          const input = span();
          const exported = toExportableSpan({ ...input,
            events: [{ name: "public-event", time: [1, 0], attributes: original }],
            links: [{ context: input.spanContext(), attributes: original }],
          }, {}, { attributes: original }, { policy });
          for (const actual of [
            mapGenkitAttributes(original, { policy }),
            mapGenkitAttributes({}, { policy, contextAttributes: original }),
            exported.events[0].attributes!, exported.links[0].attributes!,
            (exported.resource as { attributes: typeof original }).attributes,
          ]) {
            expect(actual[key]).toBe(hidden ? "__REDACTED__" : marker);
            expect(actual["service.name"]).toBe("public-service");
            expect(actual["gen_ai.tool.name"]).toBe("public-tool");
          }
        }
        expect(original[key]).toBe(marker);
      });
    }
    it(`${key} remains visible under the opposite side's policy`, () => {
      const policy = { ...SHOW_ALL, [alias === "gen_ai.tool.call.result" ? "hideInputs" : "hideOutputs"]: true };
      expect(maskAuxiliaryAttributes({ [key]: marker }, policy)[key]).toBe(marker);
    });
  }
}

it("waits for asynchronous resource attributes and masks their detached export view", async () => {
  let pending = true;
  const original = { "gen_ai.prompt": marker, "gen_ai.completion": marker, "service.name": "public-service" };
  const source = {
    get attributes() { if (pending) throw new Error("resource is not resolved"); return original; },
    get asyncAttributesPending() { return pending; },
    schemaUrl: "https://example.test/schema",
    async waitForAsyncAttributes() { pending = false; },
  };
  const exported = toExportableSpan(span(), {}, source, { policy: { ...SHOW_ALL, hideInputs: true, hideOutputs: true } });
  const resource = exported.resource as {
    attributes: Record<string, unknown>; asyncAttributesPending: boolean; schemaUrl: string; waitForAsyncAttributes(): Promise<void>;
  };
  expect(resource.asyncAttributesPending).toBe(true);
  expect(resource.attributes).toEqual({});
  await resource.waitForAsyncAttributes();
  expect(resource.asyncAttributesPending).toBe(false);
  expect(resource.schemaUrl).toBe(source.schemaUrl);
  expect(resource.attributes).toEqual({ "gen_ai.prompt": "__REDACTED__", "gen_ai.completion": "__REDACTED__", "service.name": "public-service" });
  expect(source.attributes).toBe(original);
  expect(source.attributes["gen_ai.prompt"]).toBe(marker);
  expect(source.attributes["gen_ai.completion"]).toBe(marker);
});
