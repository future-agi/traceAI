import { scrubAttribute, scrubText, credentialSecrets } from "../src/wire/privacy-runtime";
import { build, match } from "../src/wire/core";
test.each(["Authorization", "authorization_token", "api-key", "x-api-key", "xi_api_key", "api_token", "token", "access_token", "refresh_token", "id_token", "secret", "secret_key", "client_secret", "password", "private-key"])("credential key %s is redacted at every depth, including JSON tool arguments", key => {
  const value = { nested: [{ [key]: "not-for-export" }], arguments: JSON.stringify({ [key]: "not-for-export" }) };
  const result = scrubAttribute(JSON.stringify(value)); expect(result).not.toContain("not-for-export"); expect(result).toContain("<redacted>");
});
test.each(["data", "bytes", "b64_json", "file_data", "audio_base64", "images"])("inline media in %s is omitted with an exact character count", key => {
  const data = "A".repeat(300), value = key === "images" ? { images: [data] } : { nested: { [key]: data } };
  const result = scrubAttribute(JSON.stringify(value)); expect(result).not.toContain(data); expect(result).toContain("<omitted: 300 base64 chars>");
});
test("SDK error prose scrubs JSON credential/media fields even with an HTTP prefix", () => {
  const value = `401 ${JSON.stringify({ error: { authorization_token: "private-body-key", message: "data:audio/wav;base64,U0VDUkVU", source: { data: "A".repeat(300) } } })}`;
  const clean = scrubText(value);
  for (const secret of ["private-body-key", "U0VDUkVU", "A".repeat(300)]) expect(clean).not.toContain(secret);
  expect(scrubText('401 {"error":{"message":"safe"}}')).toBe('401 {"error":{"message":"safe"}}');
});
test.each([
  { audio: { format: "wav", source: { bytes: "U0VDUkVULUFVRElP" } } },
  { input_audio: { source: { nested: [{ data: "U0VDUkVULUFVRElP" }] } } },
  { source: { mimeType: "audio/wav", source: { bytes: "U0VDUkVULUFVRElP" } } },
])("embedded JSON error/stack scrubbing retains nested audio context (%j)", body => {
  const message = `400 ${JSON.stringify(body)}\n    at application (app.js:1:2)`;
  const clean = scrubText(message);
  expect(clean).not.toContain("U0VDUkVULUFVRElP"); expect(clean).toContain("<omitted: 16 base64 chars>");
  expect(clean).toMatch(/^400 /); expect(clean).toContain("\n    at application (app.js:1:2)");
});
test("embedded JSON scrubbing respects escaped quotes, braces, multiple objects and safe whitespace", () => {
  const safe = '400 { "message": "safe \\\"}\\\" [text]" }';
  expect(scrubText(safe)).toBe(safe);
  const audio = JSON.stringify({ audio: { source: { bytes: "U0VDUkVULUFVRElP" } } });
  expect(scrubText(`400 ${audio} and ${audio}`).match(/<omitted: 16 base64 chars>/g)).toHaveLength(2);
});
test.each([
  '[request: BODY]', '[outer: [request: BODY]]', '{request: BODY}', '[request: BODY] and [request: BODY]', '[request: BODY',
])("non-JSON bracketed error prose retains nested audio context (%s)", template => {
  const body = JSON.stringify({ audio: { format: "wav", source: { bytes: "U0VDUkVULUFVRElP" } } });
  const message = `invalid payload ${template.replaceAll("BODY", body)}`;
  const clean = scrubText(message);
  expect(clean).not.toContain("U0VDUkVULUFVRElP"); expect(clean).toContain("<omitted: 16 base64 chars>");
  expect(scrubAttribute(JSON.stringify({ error: { message } }))).not.toContain("U0VDUkVULUFVRElP");
  expect(scrubText(template.replaceAll("BODY", '{ "safe": "unchanged" }'))).toBe(template.replaceAll("BODY", '{ "safe": "unchanged" }'));
});
test("data URLs are omitted anywhere, including error text and tool arguments", () => {
  const data = "dGVzdA==", text = `prefix data:audio/wav;base64,${data} suffix`;
  expect(scrubText(text)).toBe("prefix <omitted: 8 base64 chars> suffix");
  expect(scrubAttribute(JSON.stringify({ arguments: JSON.stringify({ url: text }) }))).not.toContain(data);
});
test.each(["key", "api_key", "apikey", "token", "access_token", "sig", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-goog-signature", "x-goog-credential", "se", "sp", "sv", "sr", "skoid", "sktid"])("signed URL parameter %s is redacted without removing safe parameters", key => {
  expect(scrubText(`https://cdn.example/audio?${key}=private&safe=yes`)).toBe(`https://cdn.example/audio?${key}=<redacted>&safe=yes`);
});
test.each(["%74oken", "%61pi_key", "X-Amz-%53ignature", "%61ccess_token"])("percent-encoded signed parameter %s is scrubbed in prose and nested JSON", key => {
  const url = `https://cdn.example/audio?${key}=private-signed-token&safe=yes`;
  expect(scrubText(url)).toBe(`https://cdn.example/audio?${key}=<redacted>&safe=yes`);
  expect(scrubAttribute(JSON.stringify({ content: url, arguments: JSON.stringify({ url }) }))).not.toContain("private-signed-token");
  expect(scrubText("https://example.test/?%invalid=safe")).toContain("safe");
});
test.each(["inlineData", "inline_data"])("short Gemini audio in %s is omitted from recorded input", key => {
  const url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent";
  const request = { contents: [{ role: "user", parts: [{ [key]: { mimeType: "audio/wav", data: "U0VDUkVULUFVRElP" } }] }] };
  const result = build(match("POST", url)!, { method: "POST", url, headers: {}, body: Buffer.from(JSON.stringify(request)) }, { status: 200, headers: {}, size: 0 });
  expect(result).toBeDefined(); expect(JSON.stringify(result)).not.toContain("U0VDUkVULUFVRElP");
  expect(result!.attributes["input.value"]).toContain("<omitted: 16 base64 chars>");
});
test.each(["audio", "input_audio"].flatMap(key => [false, true].map(array => ({ key, array }))))("short primitive $key leaves (array $array) are omitted from JSON, error prose and wire capture", ({ key, array }) => {
  const bytes = "U0VDUkVU", audio = { [key]: array ? [bytes, bytes] : bytes }, raw = JSON.stringify(audio);
  const omitted = array ? ["<omitted: 8 base64 chars>", "<omitted: 8 base64 chars>"] : "<omitted: 8 base64 chars>";
  expect(JSON.parse(scrubAttribute(raw))).toEqual({ [key]: omitted });
  expect(scrubText(`400 ${raw}\n    at application (app.js:1:2)`)).toBe(`400 ${JSON.stringify({ [key]: omitted })}\n    at application (app.js:1:2)`);
  const url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse", request = { messages: [{ role: "user", content: [audio] }] }, before = JSON.stringify(request);
  const result = build(match("POST", url)!, { method: "POST", url, headers: {}, body: Buffer.from(before) }, { status: 200, headers: {}, size: 0 });
  expect(result).toBeDefined(); expect(JSON.stringify(result)).not.toContain(bytes);
  expect(String(result!.attributes["input.value"])).toContain("<omitted: 8 base64 chars>"); expect(JSON.stringify(request)).toBe(before);
});
test("echoed request credentials use scheme-stripped values, a minimum length and longest-first replacement", () => {
  const secrets = credentialSecrets({ Authorization: "Bearer longer-private-token", "x-api-key": "private-token", "xi-api-key": "short" }, ["configured-key"]);
  expect(scrubText("longer-private-token private-token configured-key short", secrets)).toBe("<redacted> <redacted> <redacted> short");
});
test("all extended content keys are excluded from invocation parameters", () => {
  const url = "https://api.openai.com/v1/chat/completions";
  const contentKeys = ["input", "prompt", "contents", "system", "systemInstruction", "instructions", "tools", "documents", "text", "texts", "inputs", "query", "transcript", "file", "audio_url", "url", "file_url", "image_url", "chat_history", "message", "preamble", "content", "requests"];
  const request = { model: "gpt-test", messages: [], ...Object.fromEntries(contentKeys.map(key => [key, "private-content"])) };
  const result = build(match("POST", url)!, { method: "POST", url, headers: {}, body: Buffer.from(JSON.stringify(request)) }, { status: 200, headers: {}, size: 0 });
  expect(JSON.parse(String(result?.attributes["gen_ai.request.parameters"]))).toEqual({ model: "gpt-test" });
});
test("numeric-looking credentials are still removed from recorded strings", () => {
  expect(scrubAttribute("12345678", ["12345678"])).toBe("<redacted>");
});
test("unmodified JSON tool arguments retain provider whitespace", () => {
  expect(scrubAttribute('{"city": "Oslo"}')).toBe('{"city": "Oslo"}');
});
test.each(["audio", "input_audio"])("short nested %s bytes never escape through source objects, arrays or JSON arguments", key => {
  const bytes = "U0VDUkVULUFVRElP", audio = { [key]: { format: "wav", source: { nested: [{ bytes }] } } };
  for (const value of [audio, { arguments: JSON.stringify(audio) }]) {
    const clean = scrubAttribute(JSON.stringify(value)); expect(clean).not.toContain(bytes); expect(clean).toContain("<omitted: 16 base64 chars>");
  }
  const url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse";
  const result = build(match("POST", url)!, { method: "POST", url, headers: {}, body: Buffer.from(JSON.stringify({ messages: [{ role: "user", content: [audio] }] })) }, { status: 200, headers: {}, size: 0 });
  expect(result).toBeDefined(); expect(String(result!.attributes["input.value"])).not.toContain(bytes);
});

function check(value: boolean, id: string): void { if (!value) throw new Error(id); }
test("R-C1-URL-OWNERSHIP", () => {
  const payload = String.fromCharCode(120).repeat(9);
  for (const key of ["access_token", "authorization", "cookie", "api_key", "x-amz-signature", "%74oken"]) {
    const suffix = "&safe=%2f+YES&dup=one&dup=two#anchor?token=ordinary";
    const url = `https://example.invalid/path?${key}=${payload}${suffix}`;
    const expected = `https://example.invalid/path?${key}=<redacted>${suffix}`;
    check(scrubText(url) === expected, "R-C1-URL-OWNERSHIP");
    check(scrubAttribute(url) === expected, "R-C1-URL-OWNERSHIP");
    check(scrubText(expected) === expected, "R-C1-URL-OWNERSHIP");
    check(scrubText(`see "${url}" after`) === `see "${expected}" after`, "R-C1-URL-OWNERSHIP");
    check(JSON.parse(scrubAttribute(JSON.stringify({ url }))).url === expected, "R-C1-URL-OWNERSHIP");
  }
});
test("R-C1-ASSIGNMENTS-RETAINED", () => {
  const payload = String.fromCharCode(121).repeat(9);
  for (const text of [`api_key=${payload}; safe=yes`, `authorization: Bearer ${payload}, nonce=${payload}`,
    `cookie: first=${payload}; second=${payload}`, `password="https://example.invalid/${payload}"`,
    `prefix {"access_key_id":"${payload}"}`, `prefix token='${payload}' suffix`]) {
    const clean = scrubText(text);
    check(!clean.includes(payload) && clean.includes("<redacted>"), "R-C1-ASSIGNMENTS-RETAINED");
    check(scrubText(clean) === clean, "R-C1-ASSIGNMENTS-RETAINED");
  }
});
