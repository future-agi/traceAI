import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { credentialSecrets, discoverSecrets, discoverProviderResponseSecrets, indexPrivacyHints, isCredentialName, scrubValue } from "../src/wire/privacy-runtime";
import { privacyFilter } from "../src/export-privacy";

const wire = resolve(__dirname, "../src/wire"), spec = resolve(__dirname, "../../../../spec/auto-instrumentation");
const fixtures = JSON.parse(readFileSync(resolve(wire, "privacy-fixtures.json"), "utf8"));
test("S6 runs EVERY shared golden privacy fixture, byte identity and idempotence", () => {
  for (const name of ["privacy.json", "privacy-fixtures.json"]) {
    expect(readFileSync(resolve(wire, name))).toEqual(readFileSync(resolve(spec, name)));
    for (const dir of ["dist/src/wire", "dist/esm/wire", "dist/esnext/wire"]) expect(readFileSync(resolve(__dirname, "..", dir, name))).toEqual(readFileSync(resolve(spec, name)));
  }
  // fi-core is the actual runtime reader; checking only auto/wire copies can
  // miss a stale packaged catalog behind the ESM-to-CJS privacy bridge.
  for (const dir of ["src", "dist/src", "dist/esm", "dist/esnext"]) {
    expect(readFileSync(resolve(__dirname, "../../fi-core", dir, "privacy.json"))).toEqual(readFileSync(resolve(spec, "privacy.json")));
  }
  for (const fixture of fixtures.cases) {
    const { value, path, siblings, known_secrets, site } = fixture.input;
    const hints = indexPrivacyHints(Object.entries(siblings ?? {}));
    const run = (value: unknown) => scrubValue(value, known_secrets ?? [], path, 0, false, undefined, hints, site !== "free_text_urls");
    try { expect(run(value)).toEqual(fixture.expected); expect(run(fixture.expected)).toEqual(fixture.expected); }
    catch (error) { throw new Error(`S6 fixture ${fixture.name}: ${String(error)}`); }
  }
});
test("S6 one credential predicate drives headers, query, parsed body and export keys with metric exclusions and anchored provider logprob exclusions", () => {
  const catalog = JSON.parse(readFileSync(resolve(wire, "privacy.json"), "utf8"));
  const names = [...catalog.credentials.patterns.map((p: any) => p.example), ...catalog.credentials.seed_names];
  for (const name of names) {
    const secret = "SYNTHETIC_PRIVATE_20261030";
    expect(isCredentialName(name)).toBe(true);
    expect(credentialSecrets({ [name]: secret })).toContain(secret);
    for (const source of [{ [name]: secret }, { [`metadata.${name}[0]`]: secret }, `https://example.invalid/?${encodeURIComponent(name)}=${secret}`]) expect(discoverSecrets([source])).toContain(secret);
    expect(privacyFilter().attributes({ [`metadata.${name}[0]`]: secret })[`metadata.${name}[0]`]).toBe("<redacted>");
  }
  const response = { usage: { input_tokens_details: { cache_read: "123456789" } }, choices: [{ logprobs: { content: [{ token: "ordinary-word" }] } }] };
  // Ordinary tokens are restored only at an explicitly parsed provider root.
  expect(discoverProviderResponseSecrets(response)).toEqual([]);
  // fi-security (7): identical untrusted attribute/user JSON retains the name rule.
  expect(discoverSecrets([response])).toEqual(["ordinary-word"]);
  expect(isCredentialName("secret.choices.0.logprobs.content.0.token")).toBe(true);
});
function span(attributes: any): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, name: "application", kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], attributes, status: { code: 1 }, events: [{ name: "event", attributes }], links: [{ context: ctx, attributes }], resource: { attributes }, instrumentationScope: { name: "app", attributes } };
}
test.each(["mimeType", "mime_type", "mediaType", "media_type", "contentType", "content_type"])("S6 %s array/indexed MIME hints scrub every detached export surface with exact index isolation", mime => {
  for (const hintKey of [`input.parts.0.inlineData.${mime}`, `input.parts[0].inlineData.${mime}[0]`]) for (const hint of ["audio/wav", ["text/plain", "audio/wav"]]) {
    const attrs = { "input.parts.0.inlineData.data.0": ["U0VDUkVU"], [hintKey]: hint, "input.parts.1.inlineData.data.0": "U0VDUkVU", safe: "ordinary text" };
    const original = span(attrs), before = JSON.stringify(original), clean = privacyFilter().span(original);
    for (const surface of [clean.attributes, clean.resource.attributes, (clean.instrumentationScope as any).attributes, clean.events[0].attributes!, clean.links[0].attributes!]) {
      expect(surface["input.parts.0.inlineData.data.0"]).toEqual(["<omitted: 8 base64 chars>"]);
      expect(surface["input.parts.1.inlineData.data.0"]).toBe("U0VDUkVU"); expect(surface.safe).toBe("ordinary text");
    }
    expect(JSON.stringify(original)).toBe(before);
  }
});
test("S1 preserves fi-core's already-hidden sentinel before independent S6 magic recognition", () => {
  expect(privacyFilter().attributes({ "output.value": "__REDACTED__", values: ["__REDACTED__"] })).toEqual({ "output.value": "__REDACTED__", values: ["__REDACTED__"] });
});
test.each([undefined, 4096])("S6 exact scalar data URI and returned signed/media URLs share every export surface (image cap %s)", base64ImageMaxLength => {
  const original = span({ literal: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg">payload</svg>', url: "https://example.invalid/?content=AQIDBA%3D%3D&mime_type=audio%2Fpcm&client_secret=synthetic-private&model=keep#anchor", png: "iVBORw0KGgoAAAANSUhEUg==" });
  const before = JSON.stringify(original);
  const clean = privacyFilter({ base64ImageMaxLength }).span(original);
  // F2 cap-0 whole-scope replacement precedes I1 media markers; the explicit
  // positive-cap twin retains the original S6 markers, not the media payloads.
  for (const surface of [clean.attributes, clean.resource.attributes, (clean.instrumentationScope as any).attributes, clean.events[0].attributes!, clean.links[0].attributes!]) {
    expect(surface.literal).toBe(base64ImageMaxLength === undefined ? "[image-redacted]" : "<omitted: data URI>");
    expect(surface.png).toBe(base64ImageMaxLength === undefined ? "[image-redacted]" : "<omitted: 24 base64 chars>");
    expect(surface.url).toBe("https://example.invalid/?content=<omitted: 8 base64 chars>&mime_type=audio%2Fpcm&client_secret=<redacted>&model=keep#anchor");
    for (const value of ["synthetic-private", "AQIDBA", "payload", original.attributes.png]) expect(JSON.stringify(surface)).not.toContain(value);
  }
  expect(JSON.stringify(original)).toBe(before);
});
