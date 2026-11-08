const { test } = require("node:test");
const assert = require("node:assert/strict");
const { redact, redactValue } = require("../../../dist/src/privacy.js");

test("structured redaction covers nested snake/camel credentials and header aliases without mutation", () => {
  const values = Object.fromEntries(
    [
      "api_key",
      "apiKey",
      "secretKey",
      "secret_key",
      "password",
      "access_token",
      "refreshToken",
      "authToken",
      "id_token",
      "X-Api-Key",
      "X-Secret-Key",
      "Authorization",
      "Proxy-Authorization",
      "Cookie",
      "Set-Cookie",
    ].map((key, i) => [key, `opaque_value_${i}`]),
  );
  const original = { safe: "keep this", nested: [values] };
  const clean = redactValue(original);
  assert.equal(clean.safe, original.safe);
  for (const value of Object.values(values))
    assert(!JSON.stringify(clean).includes(value));
  assert.deepEqual(original.nested[0], values);
  assert(Object.values(values).every((v) => v.startsWith("opaque_value_")));
});

test("text redaction handles quoted JSON values, escaped quotes, spaces and plain credentials", () => {
  const secret = 'opaque value with "escaped quotes" and punctuation;,';
  for (const key of [
    "api_key",
    "secretKey",
    "password",
    "access_token",
    "X-Api-Key",
    "Authorization",
  ]) {
    const clean = redact(`example ${JSON.stringify({ [key]: secret })} after`);
    assert(!clean.includes("opaque"));
    assert(!clean.includes("escaped"));
    assert(clean.includes("after"));
  }
  assert(
    !redact("password='opaque spaced value' api_key=opaque-bare").includes(
      "opaque",
    ),
  );
  assert(
    !redact("Authorization: Bearer opaque-auth-token").includes(
      "opaque-auth-token",
    ),
  );
});
