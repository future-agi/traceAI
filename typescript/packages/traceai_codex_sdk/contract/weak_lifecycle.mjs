// Deterministic GC is test-only: node --expose-gc contract/weak_lifecycle.mjs.
import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { Codex } from "@openai/codex-sdk";
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

const newClient = () => new Codex({ apiKey: "synthetic-gc-key", env: { PATH: process.env.PATH ?? "" } });
function droppedClients() {
  return Array.from({ length: 1000 }, () => {
    const client = newClient();
    client.startThread = client.startThread.bind(client);
    client.resumeThread = client.resumeThread.bind(client);
    instrumentation.wrapCodex(client);
    return new WeakRef(client);
  });
}
function droppedClientWithSavedFactories() {
  const client = newClient();
  instrumentation.wrapCodex(client);
  return { ref: new WeakRef(client), start: client.startThread, resume: client.resumeThread };
}
// Include class-patch records too: moving client bookkeeping there must not hide a leak.
const cleanupRecords = () => instrumentation.undo.length + (instrumentation.clientUndo?.size ?? 0);
const liveClient = newClient();
const descriptors = Object.getOwnPropertyDescriptors(liveClient);
instrumentation.wrapCodex(liveClient);
try {
  const saved = droppedClientWithSavedFactories();
  const liveThread = saved.start.call(liveClient);
  assert.equal(isPatched(liveThread), true, "control: the saved factory starts active");
  for (let batch = 1; batch <= 3; batch += 1) {
    const clients = [...droppedClients(), saved.ref];
    let collected = false;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      await delay(20);
      global.gc();
      await delay(20);
      collected = clients.every((ref) => ref.deref() === undefined);
      if (collected && cleanupRecords() === 1) break;
    }
    assert.equal(collected, true, "wrapped dropped clients with bound factories must remain collectable");
    assert.equal(cleanupRecords(), 1, "only the live client's undo bookkeeping remains after each batch");
    assert.equal(instrumentation.clientUndo.size, 1);
    assert.equal(isPatched(saved.start.call(liveClient)), false, "collected client's saved start factory is inert");
    assert.equal(isPatched(saved.resume.call(liveClient, "gc-resumed")), false, "collected client's saved resume factory is inert");
    assert.equal(isPatched(liveThread), true, "collecting a client must not unpatch its live threads");
    console.log(`PASS weak client batch ${batch}: ${batch * 1000} bound-factory SDK clients collected, one live cleanup record`);
  }
  instrumentation.unpatch();
  assert.deepEqual(Object.getOwnPropertyDescriptors(liveClient), descriptors);
  assert.equal(cleanupRecords(), 0);
  assert.equal(isPatched(liveThread), false);
  instrumentation.wrapCodex(liveClient);
  assert.equal(isPatched(liveClient.startThread()), true, "control: the instrumentor is enabled again");
  assert.equal(isPatched(saved.start.call(liveClient)), false, "re-enabling must not reactivate a collected client's start factory");
  assert.equal(isPatched(saved.resume.call(liveClient, "gc-resumed")), false, "re-enabling must not reactivate a collected client's resume factory");
} finally {
  instrumentation.unpatch();
}
assert.deepEqual(Object.getOwnPropertyDescriptors(liveClient), descriptors);
assert.equal(cleanupRecords(), 0);
console.log("PASS weak client lifecycle: bounded bookkeeping, inert saved factories, live threads preserved, live client restored");
