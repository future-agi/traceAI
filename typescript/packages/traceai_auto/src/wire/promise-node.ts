import { promiseHooks } from "node:v8";

interface Observation { mirror?: Promise<never> }
const handled = new WeakSet<object>();
const observations = new WeakMap<object, Observation>();
const originals = new WeakMap<object, Promise<unknown>>();
const NativePromise = Promise, reject = Promise.reject, then = Promise.prototype.then;
let internal = false, installed = false, removed = false;
let stopHook: Function | undefined, replacedEmit: typeof process.emit | undefined, previousEmit: typeof process.emit | undefined;

/**
 * A settlement tap normally marks the original rejection handled. Preserve Node's
 * native unhandled/rejectionHandled lifecycle using a native rejection mirror,
 * and v8's parent-promise hook to distinguish application continuations from ours.
 * No timers, user .then overrides, promise replacements or synthetic process events.
 */
function install(): void {
  if (installed || removed) return;
  installed = true;
  stopHook = promiseHooks.createHook({ init(_promise, parent) {
    if (!parent || internal) return;
    try {
      handled.add(parent);
      const record = observations.get(parent);
      if (record?.mirror) {
        internal = true;
        try { Reflect.apply(then, record.mirror, [undefined, () => {}]); } finally { internal = false; }
      }
    } catch { internal = false; /* A diagnostics callback must never escape into V8. */ }
  } });
  const emit = previousEmit = process.emit;
  process.emit = replacedEmit = function(this: NodeJS.Process, event: string | symbol, ...args: any[]): boolean {
    // Node still owns scheduling, listener dispatch and its default fatal action.
    // Only substitute the promise identity in the two rejection notifications.
    if (event === "unhandledRejection" && originals.has(args[1])) args[1] = originals.get(args[1]);
    else if (event === "rejectionHandled" && originals.has(args[0])) args[0] = originals.get(args[0]);
    return Reflect.apply(emit, this, [event, ...args]);
  } as typeof process.emit;
}
export function preparePromiseObservation(): void { install(); }
/** Stop the promise hook and give process.emit back. Observation ends for good. */
export function removePromiseObservation(): void {
  if (removed) return;
  removed = true;
  if (!installed) return;
  try { stopHook?.(); } catch { /* Never throw from cleanup. */ }
  // A later wrapper may delegate to ours; it keeps working, since ours only forwards.
  if (process.emit === replacedEmit && previousEmit) process.emit = previousEmit;
}
export function observePromise<T>(promise: Promise<T>, ok: (value: T) => void, fail: (error: unknown) => void): void {
  if (removed) return;
  install();
  const record: Observation = observations.get(promise) ?? {};
  observations.set(promise, record);
  internal = true;
  try {
    Reflect.apply(then, promise, [
      (value: T) => { try { ok(value); } catch { /* Observation only. */ } },
      (error: unknown) => {
        if (!handled.has(promise) && !record.mirror) {
          const mirror: Promise<never> = Reflect.apply(reject, NativePromise, [error]);
          record.mirror = mirror; originals.set(mirror, promise);
        }
        try { fail(error); } catch { /* Observation only. */ }
      },
    ]);
  } catch { /* Foreign/lazy thenables are left untouched, not started by the agent. */ }
  finally { internal = false; }
}
