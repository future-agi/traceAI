import type { Operations } from "./operations";

// CJS and ESM entrypoints (and duplicate installed copies) must agree on ownership.
// Weak collections do not retain transports or patched functions on their own.
interface PatchState {
  methods: WeakSet<object>;
  operations: WeakMap<object, Operations>;
  modules: Map<string, Set<object>>;
}
const key = Symbol.for("@traceai/mcp.transport-patches.v1");
const root = globalThis as unknown as Record<symbol, PatchState | undefined>;
const state = root[key] ?? {
  methods: new WeakSet<object>(),
  operations: new WeakMap<object, Operations>(),
  modules: new Map<string, Set<object>>(),
};
if (!root[key]) Object.defineProperty(root, key, { value: state });
export const patchedModules = state.modules;
export const wrappedMethods = state.methods;
export const transportOperations = state.operations;
