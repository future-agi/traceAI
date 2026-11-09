import { isCredentialName } from "./privacy-runtime";

/** Cross-copy protocol: keep this interface stable for embedded and installed agents. */
interface SecretRegistry {
  register(value: unknown): void;
  values(): readonly string[];
  fail?(): void;
}
const KEY = Symbol.for("traceai.secrets");
function registry(): SecretRegistry {
  const root = globalThis as typeof globalThis & { [KEY]?: SecretRegistry };
  const existing = root[KEY];
  if (existing) return existing;
  // Recency is the last registration/transport observation, not an export read.
  const lru = new Map<string, true>();
  let snapshot: readonly string[] | undefined, failed = false;
  const holder: SecretRegistry = Object.freeze({
    register(value: unknown) {
      if (typeof value !== "string" || value.length < 8 || value === "<redacted>") return;
      const known = lru.delete(value);
      lru.set(value, true);
      if (!known) snapshot = undefined;
      if (lru.size > 1024) { lru.delete(lru.keys().next().value!); snapshot = undefined; }
    },
    values() {
      if (failed) throw new Error("Privacy credential discovery incomplete");
      return snapshot ??= Object.freeze([...lru.keys()].sort((a, b) => b.length - a.length));
    },
    // Unknown credentials may be echoed by any later span. A process-wide latch,
    // not a request-local flag, makes all copies' export boundaries fail closed.
    fail() { failed = true; },
  });
  Object.defineProperty(root, KEY, { value: holder, configurable: true });
  return holder;
}
/** Register an extra known credential. Non-strings and values shorter than eight characters are ignored.
 * Values are private, process-local, and shared by all copies of fi-core and the Node agent.
 */
export function registerSecret(value: unknown): void { registry().register(value); }
/** @internal Export-boundary snapshot; never include it in diagnostics or errors. */
export function getRegisteredSecrets(): readonly string[] { return registry().values(); }
/** @internal Irrecoverable transport discovery failure: future FI exports retain only identity/timing. */
export function markSecretDiscoveryFailed(): void {
  const holder = registry();
  if (holder.fail) { holder.fail(); return; }
  // A previously loaded copy may know only register/values. Replace its public
  // holder with a fail-closed adapter, preserving the one original bounded store.
  // Older getRegisteredSecrets implementations also look up this symbol each time.
  Object.defineProperty(globalThis, KEY, { configurable: true, value: Object.freeze({
    register(value: unknown) { holder.register(value); },
    values(): readonly string[] { throw new Error("Privacy credential discovery incomplete"); },
    fail() {},
  }) });
}
/** @internal Seed once at agent initialization, including credentials passed as code options. */
export function seedSecrets(env: Record<string, string | undefined>, configured: readonly unknown[] = []): void {
  for (const value of configured) registerSecret(value);
  for (const [name, value] of Object.entries(env)) if (isCredentialName(name, "environment")) registerSecret(value);
}
