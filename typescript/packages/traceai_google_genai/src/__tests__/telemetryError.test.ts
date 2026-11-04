import { requestCredentials, telemetryError } from "../telemetryError";
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
    const copy = telemetryError(error, [key]);
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
      const copy = telemetryError(new Error(message), [credential]);
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
      const copy = telemetryError(new Error(message), []);
      expect(JSON.stringify(copy)).not.toContain(key);
      expect(copy.message).toContain(REDACTED_VALUE);
    },
  );

  it("preserves safe diagnostics and does not mistake an absent key for empty text", () => {
    const error = new TypeError("safe diagnostic");
    expect(telemetryError(error, [""])).toEqual({
      name: error.name,
      message: error.message,
      stack: error.stack,
    });
    expect(telemetryError("safe rejection", [])).toEqual({
      message: "safe rejection",
    });
  });

  it("fails closed on malformed Unicode credentials", () => {
    expect(telemetryError(new Error(key), [key + "\ud800"])).toEqual({
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
    expect(telemetryError(error, [key])).toEqual({
      message: "Google GenAI request failed",
    });
    expect(
      telemetryError(
        {
          toString() {
            throw new Error(key);
          },
        },
        [key],
      ),
    ).toEqual({ message: "Google GenAI request failed" });
  });
});

describe("effective request credential capture", () => {
  const headerSecret = "synthetic-header-secret-PRIVATE";
  const model = {
    apiKey: key,
    _requestOptions: {
      customHeaders: { Authorization: `Bearer ${headerSecret}` },
    },
  };

  it("captures model options and uses shallow per-call replacement", () => {
    expect(requestCredentials(model, {})).toEqual([
      key,
      `Bearer ${headerSecret}`,
      headerSecret,
    ]);
    expect(
      requestCredentials(model, {
        customHeaders: { "X-API-Key": "replacement" },
      }),
    ).toEqual([key, "replacement"]);
    expect(requestCredentials(model, { customHeaders: undefined })).toEqual([
      key,
    ]);
    expect(requestCredentials(model, { customHeaders: {} })).toEqual([key]);
    expect(model._requestOptions.customHeaders.Authorization).toBe(
      `Bearer ${headerSecret}`,
    );
  });

  it.each([
    {
      "pRoXy-aUtHoRiZaTiOn": `Basic ${headerSecret}`,
      "X-Request-Id": "public",
    },
    [
      ["pRoXy-aUtHoRiZaTiOn", `Basic ${headerSecret}`],
      ["X-Request-Id", "public"],
    ],
    new Headers({
      "Proxy-Authorization": `Basic ${headerSecret}`,
      "X-Request-Id": "public",
    }),
  ])(
    "handles SDK header collections and preserves unrelated diagnostics",
    (customHeaders) => {
      const credentials = requestCredentials(model, { customHeaders });
      expect(credentials).toEqual([key, `Basic ${headerSecret}`, headerSecret]);
      expect(
        telemetryError(new Error(`public ${headerSecret}`), credentials)
          .message,
      ).toBe(`public ${REDACTED_VALUE}`);
    },
  );

  it("captures sensitive values even when their header name is invalid", () => {
    const credentials = requestCredentials(model, {
      customHeaders: { " Authorization ": `Bearer ${headerSecret}` },
    });
    expect(
      JSON.stringify(telemetryError(new Error(headerSecret), credentials)),
    ).not.toContain(headerSecret);
  });

  it("snapshots values rather than retaining mutable options", () => {
    const customHeaders = { Authorization: `Bearer ${headerSecret}` };
    const credentials = requestCredentials(model, { customHeaders });
    customHeaders.Authorization = "replacement";
    expect(credentials).toContain(headerSecret);
  });

  it("removes raw and escaped copies of multiple overlapping credentials", () => {
    const secrets = [key, key + "-suffix", headerSecret + '\n"\t/=?'];
    const message = secrets
      .flatMap((value) => [
        value,
        JSON.stringify(value),
        encodeURIComponent(value),
      ])
      .join(" | ");
    const copy = telemetryError(new Error(message), secrets);
    expect(JSON.stringify(copy)).not.toContain(key);
    expect(JSON.stringify(copy)).not.toContain(headerSecret);
    expect(copy.message).not.toContain("-suffix");
  });

  it("fails closed on credential getters and malformed collections", () => {
    const hostile = {
      get customHeaders() {
        throw new Error(headerSecret);
      },
    };
    for (const options of [
      hostile,
      { customHeaders: [null] },
      {
        customHeaders: { Authorization: { toString: () => headerSecret } },
      },
    ]) {
      const credentials = requestCredentials(model, options);
      expect(credentials).toBeNull();
      expect(telemetryError(new Error(headerSecret), credentials)).toEqual({
        message: "Google GenAI request failed",
      });
    }
    expect(
      requestCredentials(
        {
          get apiKey() {
            throw new Error(key);
          },
        },
        {},
      ),
    ).toBeNull();
  });
});
