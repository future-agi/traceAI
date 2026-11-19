import type { Attributes } from "@opentelemetry/api";
import { projectAttributes } from "../src/attribute-projection";

function check(condition: boolean, id: string): void {
  if (!condition) throw new Error(id);
}
const marker = "traceai.capture.dropped_content_count";
const zeroDetails = ["gen_ai.usage.cache_read_tokens", "gen_ai.usage.input_tokens.audio", "gen_ai.usage.output_tokens.audio", "gen_ai.usage.output_tokens.reasoning"];
const coreUsage = ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens"];
const content = ["input.value", "input.mime_type", "gen_ai.request.parameters", "gen_ai.input.messages", "raw.input", "output.value", "output.mime_type", "gen_ai.output.messages", "raw.output"];
// The 23-key A7 completion shape: fourteen protected keys (including four zero
// detail counters) and nine content keys. Values are entirely synthetic.
function completion(): Attributes {
  const input = JSON.stringify({ model: "fixture-model", messages: [{ role: "user", content: "hello" }] });
  const output = JSON.stringify({ id: "response-fixture", model: "fixture-model", choices: [{ message: { role: "assistant", content: "answer" }, finish_reason: "stop" }], usage: { prompt_tokens: 101, completion_tokens: 7, total_tokens: 108 } });
  return {
    "gen_ai.span.kind": "LLM",
    "gen_ai.request.model": "fixture-model",
    "input.value": input,
    "input.mime_type": "application/json",
    "gen_ai.request.parameters": "{}",
    "gen_ai.provider.name": "openai",
    "gen_ai.operation.name": "chat",
    "gen_ai.input.messages": '[{"role":"user","content":"hello"}]',
    "raw.input": input,
    "output.value": output,
    "output.mime_type": "application/json",
    "gen_ai.response.model": "fixture-model",
    "gen_ai.response.id": "response-fixture",
    "gen_ai.response.finish_reasons": '["stop"]',
    "gen_ai.output.messages": '[{"role":"assistant","content":"answer"}]',
    "gen_ai.usage.output_tokens": 7,
    "gen_ai.usage.input_tokens": 101,
    "gen_ai.usage.total_tokens": 108,
    "gen_ai.usage.cache_read_tokens": 0,
    "gen_ai.usage.input_tokens.audio": 0,
    "gen_ai.usage.output_tokens.audio": 0,
    "gen_ai.usage.output_tokens.reasoning": 0,
    "raw.output": output,
  };
}
const kept16 = [
  "gen_ai.response.id", "gen_ai.span.kind", "gen_ai.response.finish_reasons",
  "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.model",
  "gen_ai.usage.cache_read_tokens", "gen_ai.usage.input_tokens", "gen_ai.usage.input_tokens.audio",
  "gen_ai.usage.output_tokens", "gen_ai.usage.output_tokens.audio", "gen_ai.usage.total_tokens",
  "output.mime_type", "output.value", marker,
];

test("R-F1-A7-CANONICAL-16", () => {
  const attributes = completion(), before = JSON.stringify(attributes);
  check(Object.keys(attributes).length === 23 && content.length === 9, "R-F1-A7-CANONICAL-16-CANDIDATES");
  const projected = projectAttributes(attributes, 16), kept = projected.attributes;
  check(JSON.stringify(Object.keys(kept)) === JSON.stringify(kept16), "R-F1-A7-CANONICAL-16-ORDERED-KEYS");
  check(kept["output.value"] === attributes["output.value"] && kept["output.mime_type"] === "application/json", "R-F1-A7-CANONICAL-16-OUTPUT");
  check(JSON.parse(String(kept["output.value"])).choices[0].message.content === "answer", "R-F1-A7-CANONICAL-16-PARSED-ANSWER");
  check(coreUsage.every(key => kept[key] === attributes[key]), "R-F1-A7-CANONICAL-16-USAGE");
  check(!("raw.output" in kept) && !("raw.input" in kept) && !(zeroDetails[3] in kept), "R-F1-A7-CANONICAL-16-OMITTED");
  check(zeroDetails.slice(0, 3).every(key => kept[key] === 0), "R-F1-A7-CANONICAL-16-MINIMAL-DEFERRAL");
  check(projected.dropped === 8 && kept[marker] === 7, "R-F1-A7-CANONICAL-16-COUNTS");
  check(Object.keys(kept).length <= 16 && JSON.stringify(attributes) === before, "R-F1-A7-CANONICAL-16-LIMIT-AND-IMMUTABLE");
});

test("R-F1-A7-CANONICAL-128", () => {
  const attributes = completion(), projected = projectAttributes(attributes, 128);
  check(Object.keys(projected.attributes).length === 23, "R-F1-A7-CANONICAL-128-COUNT");
  check(JSON.stringify(projected.attributes) === JSON.stringify(attributes), "R-F1-A7-CANONICAL-128-UNCHANGED");
  check(projected.dropped === 0 && !(marker in projected.attributes), "R-F1-A7-CANONICAL-128-NO-LOSS");
});

test("R-F1-A7-ORDER-MARKER", () => {
  const attributes = { ...completion(), [marker]: 5 };
  const forward = projectAttributes(attributes, 16);
  const reverse = projectAttributes(Object.fromEntries(Object.entries(attributes).reverse()), 16);
  check(JSON.stringify(reverse) === JSON.stringify(forward), "R-F1-A7-ORDER-MARKER-INSERTION-INDEPENDENT");
  check(forward.attributes[marker] === 12 && forward.dropped === 8, "R-F1-A7-ORDER-MARKER-ACCUMULATED");
  const second = projectAttributes(forward.attributes, 16);
  check(JSON.stringify(second.attributes) === JSON.stringify(forward.attributes) && second.dropped === 0, "R-F1-A7-ORDER-MARKER-IDEMPOTENT");
});

test("R-F1-A7-NONZERO-AND-UNKNOWN-DETAILS", () => {
  for (const value of [1, "0"]) {
    const attributes = completion();
    for (const key of zeroDetails) attributes[key] = value;
    const kept = projectAttributes(attributes, 16).attributes;
    check(zeroDetails.every(key => kept[key] === value), "R-F1-A7-NONZERO-AND-UNKNOWN-DETAILS-PROTECTED");
    check(coreUsage.every(key => kept[key] === attributes[key]), "R-F1-A7-NONZERO-AND-UNKNOWN-DETAILS-CORE");
    // No eligible zeros can free the pair: do not evict real usage or split MIME.
    check(!("output.value" in kept) && !("output.mime_type" in kept), "R-F1-A7-NONZERO-AND-UNKNOWN-DETAILS-ATOMIC");
    const larger = projectAttributes(attributes, 17).attributes;
    check(larger["output.value"] === attributes["output.value"] && zeroDetails.every(key => larger[key] === value), "R-F1-A7-NONZERO-AND-UNKNOWN-DETAILS-LARGER");
  }
  const attributes: Attributes = { ...completion(), "gen_ai.usage.custom_detail": 0 };
  const kept = projectAttributes(attributes, 16).attributes;
  check(kept["gen_ai.usage.custom_detail"] === 0 && kept["output.value"] === attributes["output.value"], "R-F1-A7-NONZERO-AND-UNKNOWN-DETAILS-UNKNOWN");
});

test("R-F1-A7-CORE-ZERO", () => {
  const attributes = completion();
  for (const key of coreUsage) attributes[key] = 0;
  const kept = projectAttributes(attributes, 16).attributes;
  check(coreUsage.every(key => kept[key] === 0), "R-F1-A7-CORE-ZERO-PROTECTED");
  check(kept["output.value"] === attributes["output.value"] && kept["output.mime_type"] === "application/json", "R-F1-A7-CORE-ZERO-OUTPUT");
});

test("R-F1-A7-LIMIT-BOUNDARIES", () => {
  const attributes = completion();
  for (const limit of [0, 1, 10, 11, 12, 13, 14, 15, 16, 17, 22, 23, 128]) {
    const projected = projectAttributes(attributes, limit), kept = projected.attributes;
    check(Object.keys(kept).length <= limit, "R-F1-A7-LIMIT-BOUNDARIES-LIMIT");
    check(("output.value" in kept) === ("output.mime_type" in kept), "R-F1-A7-LIMIT-BOUNDARIES-ATOMIC");
    const emitted = Object.keys(kept).filter(key => key !== marker).length;
    check(projected.dropped === 23 - emitted, "R-F1-A7-LIMIT-BOUNDARIES-ALL-DROPS");
    if (marker in kept) check(kept[marker] === content.filter(key => !(key in kept)).length, "R-F1-A7-LIMIT-BOUNDARIES-CONTENT-DROPS");
    if (limit >= 13) check(kept["output.value"] === attributes["output.value"] && coreUsage.every(key => kept[key] === attributes[key]), "R-F1-A7-LIMIT-BOUNDARIES-CANONICAL");
  }
});

test("R-F1-A7-PRIORITY-128-OVERFLOW", () => {
  const attributes = { ...completion(), ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${i}.message.content`, `ordinary message ${i}`])) };
  const projected = projectAttributes(attributes, 128), kept = projected.attributes;
  check(Object.keys(kept).length === 128 && projected.dropped === 46 && kept[marker] === 46, "R-F1-A7-PRIORITY-128-OVERFLOW-COUNTS");
  check(zeroDetails.every(key => kept[key] === 0) && coreUsage.every(key => kept[key] === attributes[key]), "R-F1-A7-PRIORITY-128-OVERFLOW-USAGE");
  check(kept["output.value"] === attributes["output.value"] && kept["output.mime_type"] === "application/json" && kept["raw.output"] === attributes["raw.output"] && kept["gen_ai.output.messages"] === attributes["gen_ai.output.messages"], "R-F1-A7-PRIORITY-128-OVERFLOW-OUTPUT");
  for (let i = 0; i < 150; i++) check((`gen_ai.input.messages.${i}.message.content` in kept) === (i >= 41), "R-F1-A7-PRIORITY-128-OVERFLOW-NEWEST-SUFFIX");
  check(!("input.value" in kept) && !("input.mime_type" in kept), "R-F1-A7-PRIORITY-128-OVERFLOW-NO-AGGREGATE");
});

test("R-F1-A7-EARLIER-OUTPUT-GROUP", () => {
  const attributes: Attributes = { ...completion(), "gen_ai.output.messages.0.message.role": "assistant", "gen_ai.output.messages.0.message.content": "answer" };
  const projected = projectAttributes(attributes, 18), kept = projected.attributes;
  check(kept["gen_ai.output.messages.0.message.role"] === "assistant" && kept["gen_ai.output.messages.0.message.content"] === "answer", "R-F1-A7-EARLIER-OUTPUT-GROUP-ATOMIC");
  check(kept["output.value"] === attributes["output.value"] && kept["output.mime_type"] === "application/json", "R-F1-A7-EARLIER-OUTPUT-GROUP-CANONICAL");
  check(Object.keys(kept).length === 18 && projected.dropped === 8 && kept[marker] === 7, "R-F1-A7-EARLIER-OUTPUT-GROUP-COUNTS");
  check(!(zeroDetails[3] in kept) && zeroDetails.slice(0, 3).every(key => kept[key] === 0), "R-F1-A7-EARLIER-OUTPUT-GROUP-MINIMAL-DEFERRAL");
});

test("R-F1-A7-UNFIT-EARLIER-OUTPUT-GROUP", () => {
  const attributes = { ...completion(), ...Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`gen_ai.output.messages.0.message.parts.${i}.text`, "ordinary words"])) };
  const projected = projectAttributes(attributes, 16), kept = projected.attributes;
  check(!Object.keys(kept).some(key => key.startsWith("gen_ai.output.messages.0.")), "R-F1-A7-UNFIT-EARLIER-OUTPUT-GROUP-ATOMIC");
  check(kept["output.value"] === attributes["output.value"] && kept["output.mime_type"] === "application/json", "R-F1-A7-UNFIT-EARLIER-OUTPUT-GROUP-CANONICAL");
  check(Object.keys(kept).length === 16 && projected.dropped === 28 && kept[marker] === 27, "R-F1-A7-UNFIT-EARLIER-OUTPUT-GROUP-COUNTS");
});
