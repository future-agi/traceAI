import { BeeAIInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";

// The real SDK and OTLP contracts are in contract.test.cjs. These small tests
// isolate patch ownership and the synchronous, identity-preserving return contract.
describe("BeeAI patch lifecycle", () => {
  const instances: BeeAIInstrumentation[] = [];
  function instrument(module: object) {
    const instance = new BeeAIInstrumentation();
    instances.push(instance);
    instance.manuallyInstrument(module);
    return instance;
  }
  afterEach(() => {
    instances.splice(0).forEach(instance => instance.disable());
    _resetPatchedStateForTesting();
  });

  it("does not mark unsupported exports as patched or mutate module namespaces", () => {
    instrument(Object.freeze({}));
    expect(isPatched()).toBe(false);
  });

  it("keeps the same lazy Run and does not invoke then", () => {
    const run = { middleware: jest.fn().mockReturnThis(), then: jest.fn() };
    class BaseAgent { run() { return run; } }
    instrument(Object.freeze({ BaseAgent }));
    expect(new BaseAgent().run()).toBe(run);
    expect(run.middleware).toHaveBeenCalledTimes(1);
    expect(run.then).not.toHaveBeenCalled();
  });

  it("patches independent framework copies without a process-wide skip flag", () => {
    class First { run() { return { middleware: jest.fn() }; } }
    class Second { run() { return { middleware: jest.fn() }; } }
    const first = First.prototype.run;
    const second = Second.prototype.run;
    const instrumentation = instrument({ BaseAgent: First });
    instrumentation.manuallyInstrument({ BaseAgent: Second });
    expect(First.prototype.run).not.toBe(first);
    expect(Second.prototype.run).not.toBe(second);
    instrumentation.disable();
    expect(First.prototype.run).toBe(first);
    expect(Second.prototype.run).toBe(second);
    expect(isPatched()).toBe(false);
    instrumentation.enable();
    expect(First.prototype.run).not.toBe(first);
    expect(Second.prototype.run).not.toBe(second);
  });

  it("does not wrap inherited methods twice or remove another instance's patch", () => {
    class BaseAgent { run() { return { middleware: jest.fn() }; } }
    class ReActAgent extends BaseAgent {}
    const first = instrument({ BaseAgent, ReActAgent });
    const wrapped = BaseAgent.prototype.run;
    const second = instrument({ BaseAgent });
    second.disable();
    expect(BaseAgent.prototype.run).toBe(wrapped);
    expect(Object.hasOwn(ReActAgent.prototype, "run")).toBe(false);
    first.disable();
    expect(BaseAgent.prototype.run).not.toBe(wrapped);
  });

  it("preserves synchronous errors and non-Run return values", () => {
    const error = new Error("application error");
    class BaseAgent { run() { throw error; } }
    instrument({ BaseAgent });
    expect(() => new BaseAgent().run()).toThrow(error);
    const promise = Promise.resolve("legacy");
    class BeeAgent { run() { return promise; } }
    instrument({ BeeAgent });
    expect(new BeeAgent().run()).toBe(promise);
  });
});
