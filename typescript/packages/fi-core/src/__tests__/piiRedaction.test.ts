import { describe, it, expect } from "@jest/globals";
import {
  redactPiiInString,
  redactPiiInValue,
} from "../trace/trace-config/piiRedaction";

describe("redactPiiInString", () => {
  it("redacts email addresses", () => {
    expect(redactPiiInString("Contact john@example.com for info")).toBe(
      "Contact <EMAIL_ADDRESS> for info",
    );
  });

  it("redacts multiple emails", () => {
    expect(redactPiiInString("From alice@test.org to bob@test.org")).toBe(
      "From <EMAIL_ADDRESS> to <EMAIL_ADDRESS>",
    );
  });

  it("redacts SSNs with dashes", () => {
    expect(redactPiiInString("SSN: 123-45-6789")).toBe("SSN: <SSN>");
  });

  it("redacts SSNs with dots", () => {
    expect(redactPiiInString("SSN: 123.45.6789")).toBe("SSN: <SSN>");
  });

  it("redacts SSNs with spaces", () => {
    expect(redactPiiInString("SSN: 123 45 6789")).toBe("SSN: <SSN>");
  });

  it("redacts credit cards with spaces", () => {
    const result = redactPiiInString("Card: 4111 1111 1111 1111");
    expect(result).toContain("<CREDIT_CARD>");
  });

  it("redacts credit cards with dashes", () => {
    const result = redactPiiInString("Card: 4111-1111-1111-1111");
    expect(result).toContain("<CREDIT_CARD>");
  });

  it("redacts US phone numbers", () => {
    const result = redactPiiInString("Call 555-123-4567 now");
    expect(result).toContain("<PHONE_NUMBER>");
  });

  it("redacts phone numbers with country code", () => {
    const result = redactPiiInString("Call +1 555-123-4567");
    expect(result).toContain("<PHONE_NUMBER>");
  });

  it("redacts phone numbers with parentheses", () => {
    const result = redactPiiInString("Call (555) 123-4567");
    expect(result).toContain("<PHONE_NUMBER>");
  });

  it("redacts IPv4 addresses", () => {
    expect(redactPiiInString("Server at 192.168.1.100 is down")).toBe(
      "Server at <IP_ADDRESS> is down",
    );
  });

  it("redacts API keys (sk-live)", () => {
    const key = "sk-live-ABCDEFGHIJKLMNOPQRSTuvwx";
    expect(redactPiiInString(`Key: ${key}`)).toBe("Key: <API_KEY>");
  });

  it("redacts API keys (pk_test)", () => {
    const key = "pk_test_ABCDEFGHIJKLMNOPQRSTuvwx";
    expect(redactPiiInString(`Key: ${key}`)).toBe("Key: <API_KEY>");
  });

  it("passes through strings with no PII", () => {
    const text = "Hello, this is a normal sentence.";
    expect(redactPiiInString(text)).toBe(text);
  });

  it("handles empty string", () => {
    expect(redactPiiInString("")).toBe("");
  });

  it("handles mixed PII", () => {
    const text = "Email john@acme.com, SSN 123-45-6789, IP 10.0.0.1";
    const result = redactPiiInString(text);
    expect(result).toContain("<EMAIL_ADDRESS>");
    expect(result).toContain("<SSN>");
    expect(result).toContain("<IP_ADDRESS>");
    expect(result).not.toContain("john@acme.com");
    expect(result).not.toContain("123-45-6789");
    expect(result).not.toContain("10.0.0.1");
  });

  it("works correctly when called multiple times (global regex reset)", () => {
    // Regression: global regexes must reset lastIndex between calls
    expect(redactPiiInString("a@b.com")).toBe("<EMAIL_ADDRESS>");
    expect(redactPiiInString("c@d.com")).toBe("<EMAIL_ADDRESS>");
    expect(redactPiiInString("e@f.com")).toBe("<EMAIL_ADDRESS>");
  });
});

describe("redactPiiInValue", () => {
  it("redacts strings", () => {
    expect(redactPiiInValue("Email: test@example.com")).toBe(
      "Email: <EMAIL_ADDRESS>",
    );
  });

  it("redacts arrays of strings", () => {
    expect(
      redactPiiInValue(["test@a.com", "no pii here", "192.168.1.1"]),
    ).toEqual(["<EMAIL_ADDRESS>", "no pii here", "<IP_ADDRESS>"]);
  });

  it("passes through numbers", () => {
    expect(redactPiiInValue(42)).toBe(42);
  });

  it("passes through booleans", () => {
    expect(redactPiiInValue(true)).toBe(true);
  });

  it("passes through undefined", () => {
    expect(redactPiiInValue(undefined)).toBeUndefined();
  });

  it("handles arrays with mixed types", () => {
    expect(redactPiiInValue(["test@a.com", 42, true])).toEqual([
      "<EMAIL_ADDRESS>",
      42,
      true,
    ]);
  });
});
