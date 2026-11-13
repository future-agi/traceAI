import { mapGenkitAttributes } from "../mapping";
import { SHOW_ALL } from "../privacy";

it("keeps native-derived kind, model and usage above context defaults", () => {
  const out = mapGenkitAttributes({
    "genkit:type": "action", "genkit:metadata:subtype": "model", "genkit:name": "offline/public",
    "genkit:output": JSON.stringify({ message: { role: "model", content: [{ text: "public reply" }] }, usage: { inputTokens: 7 } }),
  }, { policy: SHOW_ALL, contextAttributes: {
    "fi.span.kind": "TOOL", "gen_ai.span.kind": "TOOL", "gen_ai.request.model": "context-default",
    "gen_ai.usage.input_tokens": 999,
  } });
  expect(out["fi.span.kind"]).toBe("LLM");
  expect(out["gen_ai.span.kind"]).toBe("LLM");
  expect(out["gen_ai.usage.input_tokens"]).toBe(7);
  expect(out["gen_ai.request.model"]).toBe("public");
});

it("does not derive native usage from a context-only response", () => {
  const out = mapGenkitAttributes({
    "genkit:type": "action", "genkit:metadata:subtype": "model", "genkit:name": "offline/public",
  }, { policy: SHOW_ALL, contextAttributes: {
    "genkit:output": JSON.stringify({ usage: { inputTokens: 999 }, finishReason: "stop" }),
  } });
  expect(out["gen_ai.usage.input_tokens"]).toBeUndefined();
  expect(out["gen_ai.response.finish_reasons"]).toBeUndefined();
});
