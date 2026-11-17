import { diag } from "@opentelemetry/api";
import { privacyFilter } from "../export-privacy";

// The export boundary reads two switches itself: FI_HIDE_LLM_INVOCATION_PARAMETERS, and
// FI_HIDE_ERROR_DETAILS to tell a value someone set from one that is derived. Both follow
// the rule of every other privacy switch: true, 1, yes and on turn a switch on, false, 0,
// no and off turn it off (any case, surrounding spaces ignored), an empty value counts as
// unset, and any other value turns the switch on with one warning that names the variable
// and never repeats the value.

const INVOCATION = "FI_HIDE_LLM_INVOCATION_PARAMETERS";
const ERROR_TEXT = "SYNTHETIC_PROVIDER_ERROR";
const saved = { ...process.env };
let warn: jest.SpyInstance;

beforeEach(() => {
  for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  warn = jest.spyOn(diag, "warn").mockImplementation(() => undefined);
});
afterEach(() => {
  warn.mockRestore();
  process.env = { ...saved };
});

const request = () => ({
  "gen_ai.request.model": "fixture-model",
  "gen_ai.request.parameters": '{"temperature":0.25}',
  "gen_ai.request.temperature": 0.25,
});
function expectKept(exported: Record<string, unknown>): void {
  expect(JSON.parse(String(exported["gen_ai.request.parameters"]))).toEqual({ temperature: 0.25 });
  expect(exported["gen_ai.request.temperature"]).toBe(0.25);
  expect(exported["gen_ai.request.model"]).toBe("fixture-model");
}

/** A finished span whose status carries the provider's error text. */
function failed(): any {
  return { _spanContext: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 },
    name: "application", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes: { "gen_ai.request.model": "fixture-model" },
    status: { code: 2, message: ERROR_TEXT }, resource: { attributes: {} }, instrumentationScope: { name: "application" }, events: [], links: [] };
}

describe("FI_HIDE_LLM_INVOCATION_PARAMETERS at the export boundary", () => {
  it("keeps invocation parameters by default", () => {
    expectKept(privacyFilter().attributes(request()));
  });

  it.each(["true", "yes", "on", "1", " On ", "YES"])("%j hides invocation parameters", (value) => {
    process.env[INVOCATION] = value;
    const exported = privacyFilter().attributes(request());
    expect(exported["gen_ai.request.parameters"]).toBeUndefined();
    expect(exported["gen_ai.request.temperature"]).toBeUndefined();
    expect(exported["gen_ai.request.model"]).toBe("fixture-model");
  });

  it.each(["false", "no", "off", "0", " OFF ", "", "   "])("%j keeps invocation parameters", (value) => {
    process.env[INVOCATION] = value;
    expectKept(privacyFilter().attributes(request()));
  });

  it("an unrecognised value hides, with one warning that names the variable and not the value", () => {
    process.env[INVOCATION] = "maybe-later";
    for (const filter of [privacyFilter(), privacyFilter()]) {
      const exported = filter.attributes(request());
      expect(exported["gen_ai.request.parameters"]).toBeUndefined();
      expect(exported["gen_ai.request.temperature"]).toBeUndefined();
    }
    const messages = warn.mock.calls.map((call) => String(call[0]));
    const named = messages.filter((message) => message.includes(INVOCATION));
    expect(named).toHaveLength(1);
    expect(named[0]).toContain("true, 1, yes, on");
    for (const message of messages) expect(message).not.toContain("maybe-later");
  });

  it("an explicit option still wins over the environment", () => {
    process.env[INVOCATION] = "yes";
    expectKept(privacyFilter({ hideLLMInvocationParameters: false }).attributes(request()));
    process.env[INVOCATION] = "no";
    expect(privacyFilter({ hideLLMInvocationParameters: true }).attributes(request())["gen_ai.request.parameters"]).toBeUndefined();
  });

  it("hides error details too when it is turned on by an accepted value other than true", () => {
    process.env[INVOCATION] = "yes";
    const exported = privacyFilter().span(failed());
    expect(exported.name).toBe("application");
    expect(exported.status).toEqual({ code: 2 });
  });
});

describe("FI_HIDE_ERROR_DETAILS at the export boundary", () => {
  it("keeps error details by default", () => {
    const exported = privacyFilter().span(failed());
    expect(exported.name).toBe("application");
    expect(exported.status).toEqual({ code: 2, message: ERROR_TEXT });
  });

  it.each(["maybe", "true", "yes", "on", "1"])("%j hides error details", (value) => {
    process.env.FI_HIDE_ERROR_DETAILS = value;
    const exported = privacyFilter().span(failed());
    expect(exported.name).toBe("application");
    expect(exported.status).toEqual({ code: 2 });
    for (const call of warn.mock.calls) expect(String(call[0])).not.toContain("maybe");
  });

  it("hiding invocation parameters hides error details when FI_HIDE_ERROR_DETAILS is unset or empty", () => {
    for (const value of [undefined, "", "   "]) {
      if (value === undefined) delete process.env.FI_HIDE_ERROR_DETAILS;
      else process.env.FI_HIDE_ERROR_DETAILS = value;
      expect(privacyFilter({ hideLLMInvocationParameters: true }).span(failed()).status).toEqual({ code: 2 });
    }
  });

  // Someone who turned the switch off set it, whichever accepted spelling they used.
  it.each(["false", "off", "no", "0", " Off "])("%j keeps error details when invocation parameters are hidden", (value) => {
    process.env.FI_HIDE_ERROR_DETAILS = value;
    const exported = privacyFilter({ hideLLMInvocationParameters: true }).span(failed());
    expect(exported.name).toBe("application");
    expect(exported.status).toEqual({ code: 2, message: ERROR_TEXT });
  });
});
