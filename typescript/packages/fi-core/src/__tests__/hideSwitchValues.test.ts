import { diag } from "@opentelemetry/api";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import { traceConfigMetadata } from "../trace/trace-config/constants";

// A privacy switch someone set must never silently do nothing. The accepted values are
// true, 1, yes, on and false, 0, no, off (any case, surrounding spaces ignored). An empty
// value counts as unset. Any other value hides, with one warning that names the variable
// and the accepted values and never repeats the value.

const FI_ENV_KEYS = Object.values(traceConfigMetadata).map((m) => m.envKey);

describe("FI_HIDE_* and FI_PII_REDACTION values", () => {
  const savedEnv: Record<string, string | undefined> = {};
  let warn: jest.SpyInstance;
  beforeEach(() => {
    for (const key of FI_ENV_KEYS) {
      savedEnv[key] = process.env[key];
      delete process.env[key];
    }
    warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
  });
  afterEach(() => {
    warn.mockRestore();
    for (const key of FI_ENV_KEYS) {
      if (savedEnv[key] === undefined) delete process.env[key];
      else process.env[key] = savedEnv[key];
    }
  });

  it.each(["true", "1", "yes", "on", " TRUE ", "On", "YES"])("%j turns a hide switch on", (value) => {
    process.env.FI_HIDE_INPUTS = value;
    expect(generateTraceConfig().hideInputs).toBe(true);
  });

  it.each(["false", "0", "no", "off", " OFF ", "No"])("%j turns a hide switch off", (value) => {
    process.env.FI_HIDE_OUTPUTS = value;
    expect(generateTraceConfig().hideOutputs).toBe(false);
  });

  it.each(["", "   "])("%j counts as unset", (value) => {
    process.env.FI_HIDE_INPUT_MESSAGES = value;
    const config = generateTraceConfig();
    expect(config.hideInputMessages).toBe(false);
    expect(config.hideErrorDetails).toBe(false);
  });

  it("an unrecognised value hides, with one warning that names the variable and not the value", () => {
    process.env.FI_HIDE_OUTPUT_TEXT = "maybe-later";
    expect(generateTraceConfig().hideOutputText).toBe(true);
    expect(generateTraceConfig().hideOutputText).toBe(true);
    const messages = warn.mock.calls.map((call) => String(call[0]));
    const named = messages.filter((message) => message.includes("FI_HIDE_OUTPUT_TEXT"));
    expect(named).toHaveLength(1);
    expect(named[0]).toContain("true, 1, yes, on");
    for (const message of messages) expect(message).not.toContain("maybe-later");
  });

  it("an unrecognised FI_HIDE_ERROR_DETAILS hides error details, even with every content switch off", () => {
    process.env.FI_HIDE_ERROR_DETAILS = "enabled-please";
    expect(generateTraceConfig().hideErrorDetails).toBe(true);
    for (const call of warn.mock.calls) expect(String(call[0])).not.toContain("enabled-please");
  });

  it("a recognised FI_HIDE_ERROR_DETAILS still wins over the content switches", () => {
    process.env.FI_HIDE_INPUTS = "on";
    process.env.FI_HIDE_ERROR_DETAILS = "off";
    expect(generateTraceConfig().hideErrorDetails).toBe(false);
  });

  it("an unrecognised FI_PII_REDACTION turns redaction on", () => {
    process.env.FI_PII_REDACTION = "sure";
    expect(generateTraceConfig().piiRedaction).toBe(true);
  });

  it("an explicit option still wins over any environment value", () => {
    process.env.FI_HIDE_INPUTS = "maybe";
    expect(generateTraceConfig({ hideInputs: false }).hideInputs).toBe(false);
  });
});
