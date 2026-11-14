import { resolveConfig } from "../src/config";

// A privacy switch someone set must never silently do nothing. true, 1, yes and on turn it
// on, false, 0, no and off turn it off (any case, surrounding spaces ignored), and an empty
// value is unset. Any other value hides, with one warning that never repeats the value.

test.each(["on", "yes", "1", " TRUE ", "On"])("FI_HIDE_INPUTS=%j hides inputs", raw => {
  expect(resolveConfig({}, { FI_HIDE_INPUTS: raw }).traceConfig.hideInputs).toBe(true);
});

test.each(["off", "no", "0", "false", " OFF "])("FI_HIDE_OUTPUTS=%j does not hide outputs", raw => {
  expect(resolveConfig({}, { FI_HIDE_OUTPUTS: raw }).traceConfig.hideOutputs).toBe(false);
});

test.each(["", "   "])("FI_HIDE_INPUTS=%j counts as unset", raw => {
  expect(resolveConfig({}, { FI_HIDE_INPUTS: raw }).traceConfig.hideInputs).toBeUndefined();
});

test("an unrecognised switch hides, with one warning that names the variable and not the value", () => {
  const warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
  try {
    expect(resolveConfig({}, { FI_HIDE_OUTPUT_TEXT: "maybe-later" }).traceConfig.hideOutputText).toBe(true);
    expect(resolveConfig({}, { FI_HIDE_OUTPUT_TEXT: "maybe-later" }).traceConfig.hideOutputText).toBe(true);
    const lines = warn.mock.calls.map(call => String(call[0]));
    expect(lines.filter(line => line.includes("FI_HIDE_OUTPUT_TEXT"))).toHaveLength(1);
    for (const line of lines) expect(line).not.toContain("maybe-later");
  } finally {
    warn.mockRestore();
  }
});

test("an unrecognised FI_HIDE_ERROR_DETAILS hides error details even with every content switch off", () => {
  const warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
  try {
    expect(resolveConfig({}, { FI_HIDE_ERROR_DETAILS: "enabled-please" }).traceConfig.hideErrorDetails).toBe(true);
    for (const call of warn.mock.calls) expect(String(call[0])).not.toContain("enabled-please");
  } finally {
    warn.mockRestore();
  }
});

test("the warning names the source the value was read from", () => {
  const warn = jest.spyOn(console, "warn").mockImplementation(() => undefined);
  try {
    // A null option is not set, so the value comes from the environment.
    const config = resolveConfig({ traceConfig: { hideInputImages: null as any } }, { FI_HIDE_INPUT_IMAGES: "maybe-later" });
    expect(config.traceConfig.hideInputImages).toBe(true);
    const lines = warn.mock.calls.map(call => String(call[0]));
    expect(lines.filter(line => line.includes("FI_HIDE_INPUT_IMAGES"))).toHaveLength(1);
    expect(lines.filter(line => line.includes("traceConfig.hideInputImages"))).toHaveLength(0);
    for (const line of lines) expect(line).not.toContain("maybe-later");
  } finally {
    warn.mockRestore();
  }
});

test("FI_PII_REDACTION=on turns redaction on", () => {
  expect(resolveConfig({}, { FI_PII_REDACTION: "on" }).traceConfig.piiRedaction).toBe(true);
});
