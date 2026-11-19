import process from "node:process";

/** Node's native SignalWrap binds emit when the FIRST listener is registered.
 * Rebind that native dispatch without replacing/re-registering application listeners
 * or emitting spurious newListener/removeListener events. If Node changes these
 * internals, initialization rolls back instead of disturbing the application's handlers.
 */
function rebindNativeSigterm(): void {
  const start = process.rawListeners("newListener").find(fn => fn.name === "startListeningIfSignal");
  const stop = process.rawListeners("removeListener").find(fn => fn.name === "stopListeningIfSignal");
  const events = (process as any)._events;
  const saved = events && Object.getOwnPropertyDescriptor(events, "SIGTERM");
  if (!start || !stop || !saved || !saved.configurable || !("value" in saved)) throw new Error("Unsupported Node signal dispatcher");
  try {
    Object.defineProperty(events, "SIGTERM", { ...saved, value: undefined });
    Reflect.apply(stop, process, ["SIGTERM"]);
  } finally { Object.defineProperty(events, "SIGTERM", saved); }
  Reflect.apply(start, process, ["SIGTERM"]);
}

/** Gate dispatch, not listeners: EventEmitter never awaits an async listener.
 * Existing and later handlers retain identity, order, once semantics and arguments.
 * hold() decides per signal whether a flush is worth waiting for; when it says no,
 * the signal reaches the application at once.
 */
export function chainSigterm(flush: () => Promise<void>, hold: () => boolean = () => true): () => void {
  const original = process.emit;
  const sentinel = () => {};
  let removed = false, queue = Promise.resolve(), waiting = 0;
  const restore = () => {
    removed = true;
    if (process.emit === gated) process.emit = original;
    process.removeListener("SIGTERM", sentinel);
  };
  const gated = new Proxy(original, {
    apply(target, receiver, args) {
      if (receiver !== process || args[0] !== "SIGTERM" || removed) return Reflect.apply(target, receiver, args);
      // A signal never overtakes one that is already waiting for a flush.
      let held = true;
      if (!waiting) { try { held = hold(); } catch { held = false; } }
      if (!held) {
        if (process.rawListeners("SIGTERM").some(listener => listener !== sentinel)) return Reflect.apply(target, receiver, args);
        // Only our listener is left: restore the default disposition and re-deliver.
        restore(); process.kill(process.pid, "SIGTERM"); return true;
      }
      waiting++;
      queue = queue.then(async () => {
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          // Keep the signal deadline referenced: an otherwise idle process must
          // not exit successfully before its original SIGTERM disposition runs.
          await Promise.race([Promise.resolve().then(flush), new Promise<void>(resolve => { timer = setTimeout(resolve, 5000); })]);
        } catch { /* Always deliver the application's signal. */ }
        finally { if (timer) clearTimeout(timer); }
        // Deliver outside the promise chain: a throwing application handler must be
        // an uncaught exception, not an instrumentation-created unhandled rejection.
        await new Promise<void>(resolve => setImmediate(() => {
          try {
            // Resolve the current disposition at delivery, not receipt. An earlier queued
            // signal may have consumed a once listener while this signal waited for flush.
            const listeners = process.rawListeners("SIGTERM").filter(listener => listener !== sentinel);
            if (listeners.length) { for (const listener of listeners) Reflect.apply(listener, process, args.slice(1)); }
            else { restore(); process.kill(process.pid, "SIGTERM"); }
          } finally { waiting--; resolve(); }
        }));
      });
      return true;
    },
  });
  // Install emit FIRST so a newly created native handle binds the gated function.
  const hadHandlers = process.listenerCount("SIGTERM") > 0;
  process.emit = gated;
  try {
    process.prependListener("SIGTERM", sentinel);
    if (hadHandlers) rebindNativeSigterm();
  } catch (error) {
    restore();
    // Re-establish the original native binding if the rebind failed partway through.
    try { if (hadHandlers) rebindNativeSigterm(); } catch { /* Original handlers remain registered. */ }
    throw error;
  }
  return restore;
}
