import { diag } from "@opentelemetry/api";

const PATCH_RECORD = Symbol.for("@traceai/codex-sdk/client-patch-record");
const METHODS = ["startThread", "resumeThread"] as const;
type Client = Record<string | symbol, unknown>;

interface Owner {
  active: boolean;
}

interface FactoryPatch {
  method: typeof METHODS[number];
  originalDescriptor: PropertyDescriptor | undefined;
  wrapper: (...args: unknown[]) => object;
}

interface ClientPatch {
  owner: Owner;
  factories: FactoryPatch[];
}

/** Retain neither the client nor a possibly bound original factory in this closure. */
function weakUndo(ref: WeakRef<Client>, owner: Owner): () => void {
  return () => {
    // Saved wrappers must remain inert even if the instrumentor is enabled again.
    owner.active = false;
    const target = ref.deref();
    if (!target) return;
    const record = Object.getOwnPropertyDescriptor(target, PATCH_RECORD)?.value as ClientPatch | undefined;
    if (record?.owner !== owner) return;
    for (const { method, originalDescriptor, wrapper } of record.factories) {
      try {
        // Inspect the descriptor, not the value: application accessors must not run.
        if (Object.getOwnPropertyDescriptor(target, method)?.value !== wrapper) continue;
        if (originalDescriptor) Object.defineProperty(target, method, originalDescriptor);
        else delete target[method];
      } catch (error) {
        diag.debug(`@traceai/codex-sdk: factory unpatch failed: ${error}`);
      }
    }
    delete target[PATCH_RECORD];
  };
}

/** A client has one active owner, just like a Thread; duplicate owners do not stack. */
export function patchCodex(target: Client, wrapThread: (thread: object) => object): (() => void) | undefined {
  if ((target[PATCH_RECORD] as ClientPatch | undefined)?.owner.active) return undefined;
  const owner: Owner = { active: true };
  const record: ClientPatch = { owner, factories: [] };
  const undo = weakUndo(new WeakRef(target), owner);
  Object.defineProperty(target, PATCH_RECORD, { value: record, configurable: true });
  try {
    for (const method of METHODS) {
      const original = target[method];
      if (typeof original !== "function") continue;
      const originalDescriptor = Object.getOwnPropertyDescriptor(target, method);
      const wrapper = function tracedThreadFactory(this: unknown, ...args: unknown[]): object {
        const thread = original.apply(this, args);
        return owner.active ? wrapThread(thread) : thread;
      };
      Object.defineProperty(target, method, { value: wrapper, writable: true, configurable: true });
      record.factories.push({ method, originalDescriptor, wrapper });
    }
  } catch (error) {
    undo();
    throw error;
  }
  if (record.factories.length) return undo;
  undo();
  return undefined;
}
