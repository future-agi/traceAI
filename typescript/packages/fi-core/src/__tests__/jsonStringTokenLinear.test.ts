import { describe, expect, test } from "@jest/globals";
import { indexPrivacyHints, scrubValue } from "../privacy-runtime";

// One large JSON string token that holds many known secrets is read once, not once per secret.
const none = indexPrivacyHints([]);
const secret = "SECRETVALUE1", pad = "y".repeat(200);

describe("a JSON text with one large string token and many known secrets", () => {
  test("is read in time linear in its length", () => {
    const text = JSON.stringify({ a: ("x " + secret + " " + pad).repeat(8000) });
    expect(text.length).toBeGreaterThan(1_600_000);
    const started = process.hrtime.bigint();
    const out = scrubValue(text, [secret], "message", 0, false, undefined, none, true);
    const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
    expect(out).not.toContain(secret);
    expect(JSON.parse(out).a.split("<redacted>")).toHaveLength(8001);
    expect(elapsed).toBeLessThan(2000);
  });
});
