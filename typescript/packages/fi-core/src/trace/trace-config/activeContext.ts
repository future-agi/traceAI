import { AsyncLocalStorage } from "node:async_hooks";
import { context as apiContext, createContextKey, ROOT_CONTEXT, type Context } from "@opentelemetry/api";

/**
 * fi-core's own record of the active OpenTelemetry context, for applications
 * that have no OpenTelemetry context manager. There, the API's context.with
 * does nothing, so spans started inside a startActiveSpan callback would not
 * nest. This private AsyncLocalStorage carries the context instead, with `run`
 * semantics: it is active inside the callback and in everything the callback
 * awaits or starts, and not in the caller's flow after the callback returns.
 *
 * It is never registered as the API's global context manager, and nothing else
 * is registered. Every loaded copy of fi-core (CommonJS and ESM builds, a
 * preload and the application's own import) finds the same store through one
 * symbol slot on globalThis, which holds no telemetry.
 */
const ACTIVE_CONTEXT_STORE = Symbol.for("traceai.fi-core.active-context.v1");
type ActiveContextSlot = { version: 1; store: AsyncLocalStorage<Context> };

function sharedStore(): AsyncLocalStorage<Context> | undefined {
  try {
    const host = globalThis as typeof globalThis & { [ACTIVE_CONTEXT_STORE]?: ActiveContextSlot };
    const existing = host[ACTIVE_CONTEXT_STORE];
    if (existing) {
      const supported = existing.version === 1
        && typeof existing.store?.run === "function"
        && typeof existing.store?.getStore === "function";
      // An unknown slot version leaves context to the API alone, as before.
      return supported ? existing.store : undefined;
    }
    const slot: ActiveContextSlot = Object.freeze({ version: 1, store: new AsyncLocalStorage<Context>() });
    Object.defineProperty(host, ACTIVE_CONTEXT_STORE, { value: slot });
    return slot.store;
  } catch {
    return undefined;
  }
}

const store = sharedStore();

// The API has no public way to ask whether a context manager is registered, so
// test whether its context.with carries a value. Asked only when the API's
// active context is the root context, and never cached: an application can
// register its manager after fi-core is loaded.
const CONTEXT_MANAGER_PROBE = createContextKey("traceai.fi-core.active-context-probe");
function apiCarriesContext(): boolean {
  try {
    const probe = ROOT_CONTEXT.setValue(CONTEXT_MANAGER_PROBE, true);
    return apiContext.with(probe, () => apiContext.active().getValue(CONTEXT_MANAGER_PROBE) === true);
  } catch {
    // A failing manager keeps the API's own answer.
    return true;
  }
}

/**
 * The context fi-core treats as active. With an OpenTelemetry context manager
 * registered, this is exactly the API's active context. Without one, it is the
 * context of the innermost enclosing startActiveSpan callback or
 * {@link runInFIContext} call, or the root context outside them.
 */
export function activeFIContext(): Context {
  const active = apiContext.active();
  if (active !== ROOT_CONTEXT || !store || apiCarriesContext()) return active;
  try {
    return store.getStore() ?? active;
  } catch {
    return active;
  }
}

/**
 * Run fn with ctx active for fi-core, and through the API's context.with for an
 * application's context manager if it has one. The caller's context is back
 * when fn returns or throws; work fn starts keeps ctx. Registers nothing globally.
 */
export function runInFIContext<T>(ctx: Context, fn: () => T): T {
  if (!store) return apiContext.with(ctx, fn);
  return store.run(ctx, () => apiContext.with(ctx, fn));
}
