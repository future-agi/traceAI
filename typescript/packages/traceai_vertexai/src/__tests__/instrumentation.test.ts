import { VertexAIInstrumentation, isPatched } from "../instrumentation";
import { VERSION } from "../version";

describe("VertexAIInstrumentation ownership", () => {
  const instances: VertexAIInstrumentation[] = [];
  const instrument = () => {
    const instance = new VertexAIInstrumentation();
    instances.push(instance);
    return instance;
  };
  const module = () => ({ GenerativeModel: { prototype: { generateContent: jest.fn().mockResolvedValue({ response: {} }) } } });
  afterEach(() => { for (const instance of instances.splice(0)) instance.disable(); });

  it("exports the package version", () => {
    expect(VERSION).toBe(require("../../package.json").version);
    expect(instrument().instrumentationVersion).toBe(VERSION);
  });
  it("does not mark null or unsupported modules as patched", () => {
    const instance = instrument();
    instance.manuallyInstrument(null);
    instance.manuallyInstrument({});
    expect(isPatched()).toBe(false);
  });
  it("patches multiple separately loaded copies instead of using a global gate", () => {
    const instance = instrument();
    const first = module(), second = module();
    const a = first.GenerativeModel.prototype.generateContent, b = second.GenerativeModel.prototype.generateContent;
    instance.manuallyInstrument(first);
    instance.manuallyInstrument(second);
    expect(first.GenerativeModel.prototype.generateContent).not.toBe(a);
    expect(second.GenerativeModel.prototype.generateContent).not.toBe(b);
    instance.disable();
    expect(first.GenerativeModel.prototype.generateContent).toBe(a);
    expect(second.GenerativeModel.prototype.generateContent).toBe(b);
    expect(isPatched()).toBe(false);
  });
  it("does not steal another instance's wrappers or remove them on disable", () => {
    const first = instrument(), second = instrument(), sdk = module();
    const original = sdk.GenerativeModel.prototype.generateContent;
    first.manuallyInstrument(sdk);
    const wrapped = sdk.GenerativeModel.prototype.generateContent;
    second.manuallyInstrument(sdk);
    second.disable();
    expect(sdk.GenerativeModel.prototype.generateContent).toBe(wrapped);
    first.disable();
    expect(sdk.GenerativeModel.prototype.generateContent).toBe(original);
  });
  it("can patch a frozen module namespace without adding private exports", () => {
    const sdk = Object.freeze(module());
    const original = sdk.GenerativeModel.prototype.generateContent;
    const instance = instrument();
    instance.manuallyInstrument(sdk);
    expect(sdk.GenerativeModel.prototype.generateContent).not.toBe(original);
    instance.disable();
    expect(sdk.GenerativeModel.prototype.generateContent).toBe(original);
  });
  it("does not wrap the same classes twice through an ESM namespace alias", () => {
    const instance = instrument(), sdk = module();
    instance.manuallyInstrument(sdk);
    const wrapped = sdk.GenerativeModel.prototype.generateContent;
    instance.manuallyInstrument(Object.freeze({ ...sdk }));
    expect(sdk.GenerativeModel.prototype.generateContent).toBe(wrapped);
  });
  it("does not patch while disabled", () => {
    const instance = instrument(), sdk = module();
    const original = sdk.GenerativeModel.prototype.generateContent;
    instance.disable();
    instance.manuallyInstrument(sdk);
    expect(sdk.GenerativeModel.prototype.generateContent).toBe(original);
  });
  it("preserves SDK success and error identity if telemetry throws", async () => {
    const instance = instrument(), sdk = module();
    const result = { response: {} }, error = new Error("synthetic");
    sdk.GenerativeModel.prototype.generateContent.mockResolvedValueOnce(result).mockRejectedValueOnce(error);
    instance.manuallyInstrument(sdk);
    (instance as any).fiTracer.startSpan = () => { throw new Error("telemetry failure"); };
    await expect(sdk.GenerativeModel.prototype.generateContent()).resolves.toBe(result);
    await expect(sdk.GenerativeModel.prototype.generateContent()).rejects.toBe(error);
  });
});
