import { projectAttributes as coreProject, tracerAttributeLimit as coreLimit } from "@traceai/fi-core";
import { projectAttributes, tracerAttributeLimit } from "../src/attribute-projection";

test("H1 auto projection is the core implementation, including F1 canonical output at 16", () => {
  expect(projectAttributes).toBe(coreProject);
  expect(tracerAttributeLimit).toBe(coreLimit);
  const attrs = {
    "gen_ai.span.kind": "LLM", "gen_ai.response.id": "h1-response", "gen_ai.response.finish_reasons": ["stop"],
    "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": "fixture-model", "gen_ai.response.model": "fixture-model",
    "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108,
    "gen_ai.usage.cache_read_tokens": 0, "gen_ai.usage.input_tokens.audio": 0, "gen_ai.usage.output_tokens.audio": 0, "gen_ai.usage.output_tokens.reasoning": 0,
    "input.value": "hello", "input.mime_type": "text/plain", "gen_ai.request.parameters": "{}", "gen_ai.input.messages": "[]", "raw.input": "hello",
    "output.value": "answer", "output.mime_type": "text/plain", "gen_ai.output.messages": "[]", "raw.output": "answer",
  };
  const { attributes, dropped } = projectAttributes(attrs, 16);
  expect(Object.keys(attributes)).toHaveLength(16);
  expect(attributes).toMatchObject({ "output.value": "answer", "output.mime_type": "text/plain", "gen_ai.usage.input_tokens": 101,
    "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108, "traceai.capture.dropped_content_count": 7 });
  expect(attributes["raw.output"]).toBeUndefined();
  expect(attributes["gen_ai.usage.output_tokens.reasoning"]).toBeUndefined();
  expect(dropped).toBe(8);
  expect(projectAttributes(attrs, 128)).toEqual({ attributes: attrs, dropped: 0 });
});
