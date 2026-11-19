const { test } = require("node:test");
const assert = require("node:assert/strict");
const { redact, redactStructured } = require("../../../dist/src/privacy.js");

test("shared redaction protects normalized sensitive-key aliases, nested objects and encoded JSON", () => {
  const keys = [
    "secretkey",
    "aws_secret_access_key",
    "clientSecretKey",
    "secret-key-value",
    "TOKEN_VALUE",
    "password_hash",
    "passwd_value",
    "api_key_value",
    "privateKeyPem",
    "access.key.id",
    "credentials",
    "authorization_header",
    "cookie_value",
    "session_token_value",
  ];
  const credentials = Object.fromEntries(
    keys.map((key, i) => [key, `SYNTHETIC_PRIVATE_VALUE_${i}`]),
  );
  const value = {
    visible: "public",
    nested: [credentials],
    encoded: JSON.stringify(credentials),
    doubleEncoded: JSON.stringify(JSON.stringify(credentials)),
  };
  for (const clean of [
    redactStructured(value),
    redact(JSON.stringify(value)),
  ]) {
    const serialized = JSON.stringify(clean);
    assert.match(serialized, /public/);
    assert.match(serialized, /\[REDACTED\]/);
    assert.ok(!serialized.includes("SYNTHETIC_PRIVATE_VALUE_"));
  }
  assert.equal(redact("ordinary output"), "ordinary output");
  assert.equal(redact("password=synthetic-plain"), "[REDACTED]");
});

test("shared structural redaction suppresses strings beyond the processing byte budget", () => {
  const { LIMITS } = require("../../../dist/src/schema.js");
  for (const padding of [
    "x".repeat(LIMITS.ingress),
    "é".repeat(LIMITS.ingress / 2),
  ]) {
    const value = JSON.stringify({
      clientSecretKey: "SYNTHETIC_OVERSIZED_CREDENTIAL",
      padding,
    });
    assert.ok(Buffer.byteLength(value) > LIMITS.ingress);
    assert.equal(redact(value), "[REDACTED_SIZE_LIMIT]");
    assert.deepEqual(redactStructured({ encoded: value }), {
      encoded: "[REDACTED_SIZE_LIMIT]",
    });
  }
});

test("shared structural redaction bounds recursive input", () => {
  const cycle = { safe: "public" };
  cycle.next = cycle;
  assert.match(JSON.stringify(redactStructured(cycle)), /REDACTED_DEPTH_LIMIT/);
});
