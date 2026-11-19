import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { projectAttributes } from "../attribute-projection";
import { indexPrivacyHints, mediaReplacement, scrubValue } from "../privacy-runtime";
import { FITracer } from "../trace/trace-config/FITracer";

const key = "gen_ai.usage.cache_read_tokens.audio";

test.each([false, true])("audio cache counts survive admission and export with hiding=%s", async hidden => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const traceConfig = { hideInputs: hidden, hideOutputs: hidden };
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("audio-cache-count"), traceConfig });
    for (const value of [0, 2, Number.MAX_SAFE_INTEGER]) {
      expect(scrubValue(value, [], key)).toBe(value);
      expect(projectAttributes({ [key]: value }, 16).attributes[key]).toBe(value);
      const span = tracer.startSpan("cache-count", { attributes: {
        [key]: value, "input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT",
      } });
      span.end();
      const admitted = exporter.getFinishedSpans().at(-1)!;
      expect(admitted.attributes[key]).toBe(value);
      const first = privacyFilter(traceConfig).span(admitted);
      for (const exported of [first, privacyFilter(traceConfig).span(first)]) {
        expect(exported.attributes[key]).toBe(value);
        if (hidden) expect(JSON.stringify(exported.attributes)).not.toMatch(/PRIVATE_INPUT|PRIVATE_OUTPUT/);
      }
    }
  } finally { await provider.shutdown(); }
});

test.each([true, false, -1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, [2]])(
  "audio cache metric role rejects non-count %s", value => {
    expect(mediaReplacement(value, key)).toBe("<omitted: media>");
    expect(scrubValue(value, [], key)).toBe("<omitted: media>");
  },
);

test.each(["2", "12345678", "AQIDBAUGBwgJCgsM", "private audio payload!"])(
  "audio cache metric role never exempts string %s", value => {
    expect(mediaReplacement(value, key)).toMatch(/^<omitted: /);
    expect(scrubValue(value, [], key)).toMatch(/^<omitted: /);
  },
);

test("audio cache metric role retains secret, exact-key and payload precedence", () => {
  expect(scrubValue(12345678, ["12345678"], key)).toBe("<redacted>");
  expect(scrubValue(2, [], `secret.${key}`)).toBe("<redacted>");
  expect(mediaReplacement(2, `custom.${key}`)).toBe("<omitted: media>");
  expect(mediaReplacement(2, key, indexPrivacyHints([["type", "input_audio"]]))).toBe("<omitted: media>");
  expect(scrubValue({ audio: { data: { [key]: 2 } } })).toEqual({
    audio: { data: { [key]: "<omitted: media>" } },
  });
});
