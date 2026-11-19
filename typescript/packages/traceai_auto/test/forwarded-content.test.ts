import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { forwardedHiding, hideForwardedContent } from "../src/forwarded-content";

// With the forwarding switch, FI_HIDE_* also hides other AI tracers' content keys on
// forwarded application spans: the Vercel AI SDK keys that @traceai/vercel hides, and
// the flattened llm.input_messages / llm.output_messages keys.
const inputKeys = [
  "ai.prompt", "ai.prompt.messages", "ai.prompt.tools", "ai.toolCall.args", "ai.value", "ai.values", "ai.documents",
  "ai.request.text", "ai.schema", "ai.schema.description", "ai.decision.state", "ai.decision.questions",
  "gen_ai.prompt.0.content", "llm.prompts", "traceloop.entity.input", "gen_ai.tool.call.arguments", "tool.parameters",
  "reranker.query", "reranker.input_documents", "embedding.embeddings.0.embedding.text",
  "llm.input_messages.0.message.content", "llm.input_messages.1.message.contents.0.message_content.text",
];
const outputKeys = [
  "ai.response.text", "ai.response.object", "ai.response.toolCalls", "ai.response.reasoning", "ai.toolCall.result",
  "ai.decision.answers", "ai.ranking", "gen_ai.completion.0.content", "traceloop.entity.output", "gen_ai.tool.call.result",
  "message.tool_calls", "reranker.output_documents", "llm.output_messages.0.message.content",
];
const kept = { "ai.response.model": "gpt-fixture", "ai.response.finishReason": "stop", "app.order": "order-1", "llm.token_count.total": 6 };

function span(attributes: Record<string, unknown>): ReadableSpan {
  return { name: "application", attributes, events: [{ name: "event", time: [0, 0], attributes: { ...attributes } }] } as unknown as ReadableSpan;
}

test.each([
  { name: "inputs", options: { hideInputs: true }, hidden: inputKeys, visible: outputKeys },
  { name: "outputs", options: { hideOutputs: true }, hidden: outputKeys, visible: inputKeys },
])("hiding $name hides every listed foreign content key and keeps the rest", ({ options, hidden, visible }) => {
  const attributes = Object.fromEntries([...inputKeys, ...outputKeys].map(key => [key, `PRIVATE ${key}`]));
  const original = span({ ...attributes, ...kept });
  const view = hideForwardedContent(original, forwardedHiding(options)!);
  for (const surface of [view.attributes, view.events[0].attributes!]) {
    for (const key of hidden) expect([key, surface[key]]).toEqual([key, "[REDACTED]"]);
    for (const key of visible) expect(surface[key]).toBe(`PRIVATE ${key}`);
    expect(surface).toMatchObject(kept);
  }
  // The application's span is unchanged.
  expect(original.attributes["ai.value"]).toBe("PRIVATE ai.value");
});

test("provider metadata is hidden by either flag", () => {
  for (const options of [{ hideInputs: true }, { hideOutputs: true }]) {
    const view = hideForwardedContent(span({ "ai.response.providerMetadata": "PRIVATE metadata" }), forwardedHiding(options)!);
    expect(view.attributes["ai.response.providerMetadata"]).toBe("[REDACTED]");
  }
});
