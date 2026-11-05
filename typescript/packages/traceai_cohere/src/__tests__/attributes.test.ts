import { generateTraceConfig } from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { inputAttributes, outputAttributes } from "../attributes";

it.each(["userAccessToken", "user_access_token"])("omits %s from telemetry without changing any connector", (field) => {
  const authenticated = Object.freeze({ id: "private-connector", [field]: "synthetic-token" });
  const publicConnector = Object.freeze({ id: "web-search", options: { site: "example.test" } });
  const request = Object.freeze({ message: "question", connectors: Object.freeze([authenticated, publicConnector]) });
  const attrs = inputAttributes("chat", request, generateTraceConfig());
  expect(JSON.stringify(attrs)).not.toContain("synthetic-token");
  expect(JSON.parse(attrs[S.RAW_INPUT] as string).connectors).toEqual([
    { id: "private-connector" }, publicConnector,
  ]);
  expect(request.connectors[0]).toBe(authenticated);
  expect(authenticated[field]).toBe("synthetic-token");
});

it.each([
  { cachedTokens: 0, tokens: { inputTokens: 42, outputTokens: 9 }, billedUnits: { inputTokens: 30, outputTokens: 7, searchUnits: 0, classifications: 2, images: 1, imageTokens: 8 } },
  { cached_tokens: 0, tokens: { input_tokens: 42, output_tokens: 9 }, billed_units: { input_tokens: 30, output_tokens: 7, search_units: 0, classifications: 2, images: 1, image_tokens: 8 } },
])("retains numeric usage independently of privacy for either field spelling (%j)", usage => {
  for (const response of [{ usage }, { meta: usage }]) {
    const attrs = outputAttributes("chat", response, generateTraceConfig({ hideInputs: true, hideOutputs: true }));
    expect(attrs).toMatchObject({
      [S.LLM_TOKEN_COUNT_PROMPT]: 42, [S.LLM_TOKEN_COUNT_COMPLETION]: 9, [S.LLM_TOKEN_COUNT_TOTAL]: 51,
      [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: 0,
      "cohere.usage.billed_units.input_tokens": 30, "cohere.usage.billed_units.output_tokens": 7,
      "cohere.usage.billed_units.search_units": 0, "cohere.usage.billed_units.classifications": 2,
      "cohere.usage.billed_units.images": 1, "cohere.usage.billed_units.image_tokens": 8,
    });
  }
});

it("does not invent billable/cache counts or mix billed units into partial actual counts", () => {
  const config = generateTraceConfig({ hideOutputs: true });
  expect(outputAttributes("chat", {}, config)).toEqual({});
  expect(outputAttributes("chat", { usage: { tokens: { inputTokens: 5 }, billedUnits: { inputTokens: 3, outputTokens: 2 } } }, config)).toEqual({
    [S.LLM_TOKEN_COUNT_PROMPT]: 5, [S.LLM_TOKEN_COUNT_TOTAL]: 5,
    "cohere.usage.billed_units.input_tokens": 3, "cohere.usage.billed_units.output_tokens": 2,
  });
  expect(outputAttributes("chat", { meta: { billedUnits: { inputTokens: 3 } } }, config)).toEqual({
    [S.LLM_TOKEN_COUNT_PROMPT]: 3, [S.LLM_TOKEN_COUNT_TOTAL]: 3, "cohere.usage.billed_units.input_tokens": 3,
  });
});

it.each([undefined, null, "11", -1, NaN, Infinity])("omits invalid numeric metadata (%s)", value => {
  expect(outputAttributes("chat", { usage: {
    cachedTokens: value, tokens: { inputTokens: value, outputTokens: value },
    billedUnits: { inputTokens: value, outputTokens: value, searchUnits: value },
  } }, generateTraceConfig({ hideOutputs: true }))).toEqual({});
});
