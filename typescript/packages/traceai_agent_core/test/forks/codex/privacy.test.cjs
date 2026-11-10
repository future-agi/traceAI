const { test } = require("node:test");
const assert = require("node:assert/strict");
const { redact, redactValue } = require("../../../dist/src/privacy.js");

test("credential aliases share a policy across fields, JSON, and assignments", () => {
  for (const key of [
    "password",
    "TOKEN",
    "API_KEY",
    "x-api-key",
    "secret_key",
    "secret-access-key",
    "AWS_SECRET_ACCESS_KEY",
    "refresh_token",
    "proxy-authorization",
  ]) {
    const marker = "synthetic-private-value";
    assert.equal(redactValue({ [key]: marker })[key], "[REDACTED]");
    for (const value of [
      marker,
      `"${marker} with spaces"`,
      `'${marker} with spaces'`,
    ]) {
      const result = redact(`before ${key}=${value} after`);
      assert(!result.includes(marker));
      assert(result.includes("before"));
      assert(result.includes("after"));
    }
    assert(
      !redact(`Response: ${JSON.stringify({ [key]: marker })}`).includes(
        marker,
      ),
    );
  }
});

test("escaped and unterminated quoted credentials do not expose their suffix", () => {
  for (const value of [
    '"before\\"synthetic-suffix"',
    "'before\\'synthetic-suffix'",
    '"synthetic-suffix',
    "'synthetic-suffix",
  ]) {
    assert(!redact(`password=${value}`).includes("synthetic-suffix"));
  }
  assert.equal(
    redact("monkey=value tokenizer=public non-secret_key=public"),
    "monkey=value tokenizer=public non-secret_key=public",
  );
});
