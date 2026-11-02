import { telemetryError } from "../telemetryError";
import { REDACTED_VALUE } from "@traceai/fi-core";

const key = "synthetic-api-secret-PRIVATE";

describe("telemetry-only error sanitization", () => {
  it("redacts name, message and stack without changing the application error", () => {
    const error = new Error(`safe diagnostic ${key}`);
    error.name = `ProviderError ${key}`;
    const original = {
      name: error.name,
      message: error.message,
      stack: error.stack,
    };
    const copy = telemetryError(error, key);
    expect(copy).not.toBe(error);
    expect(JSON.stringify(copy)).not.toContain(key);
    expect(copy.message).toContain("safe diagnostic");
    expect(copy.stack).toContain(REDACTED_VALUE);
    expect({
      name: error.name,
      message: error.message,
      stack: error.stack,
    }).toEqual(original);
  });

  it.each([key, key + "\ninvalid", key + "/+?=", " " + key + " "])(
    "removes raw, trimmed, JSON-escaped and URL-encoded credentials: %p",
    (credential) => {
      const message = [
        credential,
        credential.trim(),
        JSON.stringify(credential),
        encodeURIComponent(credential),
      ].join(" | ");
      const copy = telemetryError(new Error(message), credential);
      expect(JSON.stringify(copy)).not.toContain(key);
      expect(copy.message).toContain(REDACTED_VALUE);
    },
  );

  it.each([
    `https://example.invalid/model?key=${key}&safe=1`,
    `https://example.invalid/model?safe=1&API_KEY=${key}#fragment`,
    `https://example.invalid/model?access_token=${key}`,
    `X-Goog-Api-Key: ${key}\nsafe diagnostic`,
    `Authorization: Bearer ${key}\nsafe diagnostic`,
    `Proxy-Authorization: Basic ${key}`,
    `{"x-api-key":"${key}"}`,
    `{'api_key':'${key}'}`,
  ])(
    "redacts sensitive URL/header fields even without a known key: %s",
    (message) => {
      const copy = telemetryError(new Error(message), undefined);
      expect(JSON.stringify(copy)).not.toContain(key);
      expect(copy.message).toContain(REDACTED_VALUE);
    },
  );

  it("preserves safe diagnostics and does not mistake an absent key for empty text", () => {
    const error = new TypeError("safe diagnostic");
    expect(telemetryError(error, "")).toEqual({
      name: error.name,
      message: error.message,
      stack: error.stack,
    });
    expect(telemetryError("safe rejection", undefined)).toEqual({
      message: "safe rejection",
    });
  });

  it("fails closed on malformed Unicode credentials", () => {
    expect(telemetryError(new Error(key), key + "\ud800")).toEqual({
      message: "Google GenAI request failed",
    });
  });

  it("fails closed when exception accessors or string conversion throw", () => {
    const error = new Error(key);
    Object.defineProperty(error, "message", {
      get() {
        throw new Error(key);
      },
    });
    expect(telemetryError(error, key)).toEqual({
      message: "Google GenAI request failed",
    });
    expect(
      telemetryError(
        {
          toString() {
            throw new Error(key);
          },
        },
        key,
      ),
    ).toEqual({ message: "Google GenAI request failed" });
  });
});
