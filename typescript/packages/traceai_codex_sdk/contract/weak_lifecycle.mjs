// Deterministic GC is test-only: node --expose-gc contract/weak_lifecycle.mjs.
import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { CodexSDKInstrumentation, isPatched } from "../dist/esm/index.js";

assert.equal(typeof global.gc, "function", "run this contract with --expose-gc");
const instrumentation = new CodexSDKInstrumentation();
function droppedThreads() {
  return Array.from({ length: 1000 }, () => {
    const thread = { async *runStreamedInternal() { yield { type: "turn.completed" }; } };
    // Retaining an original bound method in an undo closure would retain this
    // thread, even if the target itself were nominally stored as a WeakRef.
    thread.runStreamedInternal = thread.runStreamedInternal.bind(thread);
    instrumentation.wrapThread(thread);
    return new WeakRef(thread);
  });
}
const refs = droppedThreads();
const survivor = { async *runStreamedInternal() { yield { type: "turn.completed" }; } };
const original = survivor.runStreamedInternal;
instrumentation.wrapThread(survivor);
try {
  let collected = false;
  for (let attempt = 0; attempt < 50; attempt += 1) {
    // A WeakRef's target stays alive until the end of the job that dereferenced
    // it. Yield before each collection, not just after global.gc().
    await delay(20);
    global.gc();
    await delay(20);
    collected = refs.every((ref) => ref.deref() === undefined);
    if (collected && instrumentation.threadUndo.size === 1) break;
  }
  assert.equal(collected, true, "wrapped dropped threads must remain collectable");
  assert.equal(instrumentation.threadUndo.size, 1, "only the live thread's undo bookkeeping remains");
  assert.equal(isPatched(survivor), true);
} finally {
  instrumentation.unpatch();
}
assert.equal(survivor.runStreamedInternal, original);
assert.equal(isPatched(survivor), false);
assert.equal(instrumentation.threadUndo.size, 0);
console.log("PASS weak lifecycle: 1000 bound-method threads collected, live thread restored, undo bookkeeping reclaimed");
