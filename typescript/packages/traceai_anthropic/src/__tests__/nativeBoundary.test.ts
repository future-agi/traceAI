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
// require("@anthropic-ai/sdk") is a callable wrapper whose own prototype is a plain object.
const cjsModuleObject = Object.assign(function anthropic() {}, { default: NativeSDK, Anthropic: NativeSDK });

describe("native SDK boundary", () => {
  it.each([
    ["constructor", NativeSDK],
    ["subclass", DerivedSDK],
    ["module namespace", { Anthropic: NativeSDK, Messages: NativeSDK.Messages }],
    ["CommonJS module object", cjsModuleObject],
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

describe("legacy SDK module object", () => {
  it("patches Messages found on the default export of a CommonJS module object", () => {
    class LegacySDK {
      static Messages = class {
        create() { return "original"; }
      };
    }
    const moduleObject = Object.assign(function anthropic() {}, { default: LegacySDK, Anthropic: LegacySDK });
    const error = jest.spyOn(diag, "error").mockImplementation(() => {});
    const original = LegacySDK.Messages.prototype.create;
    const instrumentation = new AnthropicInstrumentation();
    try {
      instrumentation.manuallyInstrument(moduleObject as unknown as typeof Anthropic);
      expect(error).not.toHaveBeenCalled();
      expect(LegacySDK.Messages.prototype.create).not.toBe(original);
      expect(isPatched()).toBe(true);
    } finally {
      instrumentation.disable();
      error.mockRestore();
    }
  });
});
