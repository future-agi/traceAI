const { test } = require("node:test");
const assert = require("node:assert/strict");
const { redact, redactStructured } = require("../dist/src/privacy.js");

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
  const { LIMITS } = require("../dist/src/schema.js");
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

test("YAML block scalars under sensitive keys are redacted whole (literal, folded, nested, JSON-encoded)", () => {
  const cases = [
    "access_token: |\n  MULTILINE_CANARY",
    "access_token: >\n  MULTILINE_CANARY\n  MULTILINE_CANARY_2",
    "access_token: |-\n  MULTILINE_CANARY\n\n  MULTILINE_CANARY_2",
    "service:\n  password: >+  # comment\n    MULTILINE_CANARY\n  name: kept",
    "client_secret: |2\n    MULTILINE_CANARY\r\n    MULTILINE_CANARY_2\r\n",
  ];
  for (const text of cases) {
    const out = redact(text);
    assert.ok(!out.includes("MULTILINE_CANARY"), JSON.stringify(out));
  }
  // Following non-secret content at the parent indentation survives.
  assert.ok(redact(cases[3]).includes("name: kept"));
  assert.ok(redact("api_key: |\n  MULTILINE_CANARY\nmodel: gpt").includes("model: gpt"));
  // Escaped (JSON-string) form and structured values.
  const escaped = JSON.stringify({ config: "x: 1\naccess_token: |\n  MULTILINE_CANARY\ny: 2" });
  assert.ok(!redact(escaped).includes("MULTILINE_CANARY"), redact(escaped));
  const nested = redactStructured({ tool: { output: "token: >\n  MULTILINE_CANARY" } });
  assert.ok(!JSON.stringify(nested).includes("MULTILINE_CANARY"), JSON.stringify(nested));
});

test('sensitive YAML plain scalars redact all inline words, preserving line boundaries and shell assignment semantics', () => {
  for (const newline of ['\n', '\r\n']) {
    assert.equal(redact('password: first SYNTHETIC_PRIVATE_SUFFIX'), '[REDACTED]');
    assert.equal(redact(`password: first SYNTHETIC_PRIVATE_SUFFIX${newline}`), `[REDACTED]${newline}`);
    const text = `service:${newline}  passwd: first SYNTHETIC_PRIVATE_SUFFIX${newline}  public: retained`;
    assert.equal(redact(text), `service:${newline}  [REDACTED]${newline}  public: retained`);
    const folded = `password: first SYNTHETIC_PRIVATE_SUFFIX${newline}  FOLDED_SECRET_SUFFIX${newline}public: retained`;
    assert.equal(redact(folded), `[REDACTED]${newline}public: retained`);
  }
  assert.equal(redact('PASSWORD=synthetic-value public-command --flag'), '[REDACTED] public-command --flag');
});

test("a non-sensitive key on the previous line never hides the next sensitive key", () => {
  for (const text of ["service:\n  password: abc123SECRET", "a: 1\nb:\n  api_key: abc123SECRET", "{\"name\": \"x\",\n \"token\": abc123SECRET}"]) {
    assert.ok(!redact(text).includes("abc123SECRET"), JSON.stringify(redact(text)));
  }
});
