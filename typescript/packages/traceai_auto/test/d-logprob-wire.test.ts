import * as wire from "../src/wire/privacy-runtime";
import * as core from "@traceai/fi-core/privacy";
import { build, match } from "../src/wire/core";

const body = (token: string) => ({ choices: [{ index: 0, message: { content: "safe" }, logprobs: { content: [{ token, bytes: [65, 66], logprob: -0.2, top_logprobs: [{ token, bytes: [65, 66] }] }] } }] });

test("R-D-WIRE-SHARED: auto uses the same provider-root policy and masks token byte aliases", () => {
  const api = wire as any, shared = core as any;
  expect(api.scrubProviderResponse).toBeDefined();
  expect(api.scrubProviderResponse).toBe(shared.scrubProviderResponse);
  expect(api.discoverProviderResponseSecrets).toBe(shared.discoverProviderResponseSecrets);
  const secret = "synthetic-d-wire-private";
  const safe = api.scrubProviderResponse(body("Hello."));
  expect(safe.choices[0].logprobs.content[0].token).toBe("Hello.");
  expect(api.discoverProviderResponseSecrets(body("ordinary-word"))).toEqual([]);
  expect(wire.discoverSecrets([body("ordinary-word")])).toEqual(["ordinary-word"]);
  const privateOutput = api.scrubProviderResponse(body(secret), [secret]);
  for (const item of [privateOutput.choices[0].logprobs.content[0], privateOutput.choices[0].logprobs.content[0].top_logprobs[0]]) {
    expect(item.token).toBe("<redacted>");
    expect(item.bytes).toBe("<redacted>");
  }
});

test("R-D-WIRE-ANCHOR: parsed user/tool content never becomes a provider response root", () => {
  const url = "https://api.openai.com/v1/chat/completions";
  const nested = JSON.stringify(body("nested"));
  const request = { model: "synthetic", messages: [{ role: "user", content: nested }] };
  const response = { model: "synthetic", choices: [{ index: 0, message: { role: "assistant", content: nested }, finish_reason: "stop" }] };
  const result = build(match("POST", url)!, { method: "POST", url, headers: {}, body: Buffer.from(JSON.stringify(request)) }, {
    status: 200, headers: { "content-type": "application/json" }, body: Buffer.from(JSON.stringify(response)), size: 0,
  });
  expect(result).toBeDefined();
  expect(result!.attributes["output.value"]).toBeDefined();
  expect(JSON.parse(result!.attributes["output.value"] as string).choices[0].logprobs.content[0].token).toBe("<redacted>");
  expect(JSON.stringify(result)).not.toContain('"token":"nested"');
});
