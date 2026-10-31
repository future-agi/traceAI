import { InstructorInstrumentation, isPatched } from "../instrumentation";
import { VERSION } from "../version";

const instances: InstructorInstrumentation[] = [];
function make() {
  const inst = new InstructorInstrumentation({ instrumentationConfig: { enabled: false } });
  instances.push(inst);
  return inst;
}
afterEach(() => { for (const inst of instances.splice(0)) inst.disable(); });

it("retains the public instrumentor name and release-managed version", () => {
  const inst = make();
  expect(VERSION).toMatch(/^\d+\.\d+\.\d+$/);
  expect(inst.instrumentationName).toBe("@traceai/fi-instrumentation-instructor");
  expect(inst.instrumentationVersion).toBe(VERSION);
});

it("ignores null and unsupported modules without claiming to patch them", () => {
  const inst = make();
  inst.manuallyInstrument(null);
  inst.manuallyInstrument({ Instructor: {} });
  expect(isPatched()).toBe(false);
});

it("does not let a second instrumentor remove the first one's patch", () => {
  const first = make(); const second = make();
  const original = () => ({ chat: { completions: { create: async () => ({}) } } });
  const module = { default: original };
  first.manuallyInstrument(module); const wrapped = module.default;
  second.manuallyInstrument(module); second.disable();
  expect(module.default).toBe(wrapped); expect(isPatched()).toBe(true);
  first.disable(); expect(module.default).toBe(original); expect(isPatched()).toBe(false);
});

it("preserves a later factory wrapper on disable", () => {
  const inst = make(); const module = { default: () => ({}) };
  inst.manuallyInstrument(module);
  const wrapped = module.default;
  const later = () => wrapped(); module.default = later;
  inst.disable(); expect(module.default).toBe(later);
  expect(isPatched()).toBe(false);
});

it("restores the legacy prototype route without retaining created clients", () => {
  const inst = make(); const original = async () => ({});
  const module = { Instructor: { prototype: { chat: { completions: { create: original } } } } };
  inst.manuallyInstrument(module);
  expect(module.Instructor.prototype.chat.completions.create).not.toBe(original);
  inst.disable(); expect(module.Instructor.prototype.chat.completions.create).toBe(original);
});

it("can instrument a factory again after disabling", () => {
  const inst = make(); const original = () => ({}); const module = { default: original };
  inst.manuallyInstrument(module); inst.disable();
  inst.enable(); inst.manuallyInstrument(module);
  expect(module.default).not.toBe(original); expect(isPatched()).toBe(true);
  inst.disable(); expect(module.default).toBe(original);
});
