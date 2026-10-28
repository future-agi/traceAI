/**
 * Shape guard against the installed @cursor/sdk (devDependency, CJS build).
 * No network: it only inspects the class and stubs Agent.create.
 */
import { CursorSDKInstrumentation, isPatched } from "../index";

type Sdk = { Agent: Record<string, unknown> & { create: (...a: unknown[]) => Promise<unknown>; prompt: (...a: unknown[]) => Promise<unknown> } };

let sdk: Sdk | undefined;
let loadError: unknown;
try {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  sdk = require("@cursor/sdk");
} catch (error) {
  loadError = error;
}

const maybe = sdk ? describe : describe.skip;

maybe("installed @cursor/sdk", () => {
  it("exposes Agent.create / resume / prompt as writable, configurable statics", () => {
    for (const method of ["create", "resume", "prompt"]) {
      const descriptor = Object.getOwnPropertyDescriptor(sdk!.Agent, method);
      expect(typeof descriptor?.value).toBe("function");
      expect(descriptor?.writable).toBe(true);
      expect(descriptor?.configurable).toBe(true);
    }
  });

  it("Agent.prompt() calls Agent.create() through the class (so it is traced without its own wrapper)", async () => {
    const Agent = sdk!.Agent;
    const original = Agent.create;
    const sentinel = new Error("sentinel: prompt went through Agent.create");
    Agent.create = async () => {
      throw sentinel;
    };
    try {
      await expect(Agent.prompt("hi", { model: { id: "auto" } })).rejects.toMatchObject({ message: sentinel.message });
    } finally {
      Agent.create = original;
    }
  });

  it("manuallyInstrument() patches and unpatch() restores the real class", () => {
    const instrumentation = new CursorSDKInstrumentation();
    const original = sdk!.Agent.create;
    expect(instrumentation.manuallyInstrument(sdk as never)).toBe(true);
    expect(isPatched(sdk!.Agent)).toBe(true);
    expect(sdk!.Agent.create).not.toBe(original);
    instrumentation.unpatch();
    instrumentation.disable();
    expect(sdk!.Agent.create).toBe(original);
    expect(isPatched(sdk!.Agent)).toBe(false);
  });
});

if (!sdk) {
  it("skips the shape guard when @cursor/sdk cannot load here", () => {
    expect(loadError).toBeDefined();
  });
}
