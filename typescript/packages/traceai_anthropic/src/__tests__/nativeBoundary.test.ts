import Anthropic from "@anthropic-ai/sdk";
import { diag } from "@opentelemetry/api";
import { AnthropicInstrumentation, isPatched } from "../instrumentation";

// The check must never invoke an instance getter on a prototype (SDK getters
// may access private fields). Native telemetry being disabled is still native.
class NativeSDK {
  static Messages = class {
    create() { return "original"; }
  };
  get openTelemetry(): never { throw new Error("do not call a client getter"); }
}
class DerivedSDK extends NativeSDK {}

describe("native SDK boundary", () => {
  it.each([
    ["constructor", NativeSDK],
    ["subclass", DerivedSDK],
    ["module namespace", { Anthropic: NativeSDK, Messages: NativeSDK.Messages }],
  ])("leaves the %s unmodified and warns about privacy configuration", (_label, sdk) => {
    const warn = jest.spyOn(diag, "warn").mockImplementation(() => {});
    const original = NativeSDK.Messages.prototype.create;
    const instrumentation = new AnthropicInstrumentation();
    try {
      instrumentation.manuallyInstrument(sdk as unknown as typeof Anthropic);
      instrumentation.manuallyInstrument(sdk as unknown as typeof Anthropic);
      expect(NativeSDK.Messages.prototype.create).toBe(original);
      expect(isPatched()).toBe(false);
      expect(sdk).not.toHaveProperty("fiPatched");
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("traceConfig"));
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("native SDK spans"));
    } finally {
      instrumentation.disable();
      warn.mockRestore();
    }
    expect(NativeSDK.Messages.prototype.create).toBe(original);
  });
});
