import { privacyFilter } from "../export-privacy";
import { registerSecret } from "../secrets";
import * as runtime from "../privacy-runtime";

function fixture() {
  const context = { traceId: "3".repeat(32), spanId: "4".repeat(16), traceFlags: 1 };
  const payload = { choices: [{ index: 0, message: { content: "A safe reexport-only answer." }, logprobs: { content: [{ token: "Hello.", bytes: [72, 101], logprob: -0.1 }] } }], vectors: [0.1234567] };
  const attributes = { "raw.output": JSON.stringify(payload), safe: "safe metadata" };
  (runtime as any).rememberProviderResponse?.(context, attributes, "openai.chat");
  return { _spanContext: context, name: "chat", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "test" }, events: [], links: [] } as any;
}

test("R-D-REEXPORT: detached snapshots keep bounded origin, recheck current secrets and preserve safe tokens", () => {
  const filter = privacyFilter();
  const first = filter.span(fixture());
  const second = filter.span(first);
  expect(JSON.parse(first.attributes["raw.output"] as string).choices[0].logprobs.content[0].token).toBe("Hello.");
  expect(second.attributes).toEqual(first.attributes);
  registerSecret("A safe reexport-only answer.");
  const third = filter.span(second);
  const payload = JSON.parse(third.attributes["raw.output"] as string);
  expect(payload.choices[0].message.content).toBe("<redacted>");
  expect(payload.choices[0].logprobs).toBe("<redacted>");
});

test("R-D-POLICY: opaque vector and output policies remain authoritative for bound provider roots", () => {
  for (const options of [{ hideEmbeddingVectors: true }, { hideOutputs: true }]) {
    const original = fixture();
    const clean = privacyFilter(options).span(original);
    expect(clean.attributes.safe).toBe("safe metadata");
    expect(String(clean.attributes["raw.output"])).not.toContain("0.1234567");
    expect(String(clean.attributes["raw.output"])).not.toContain("Hello.");
    expect(privacyFilter(options).span(clean).attributes).toEqual(clean.attributes);
  }
});
