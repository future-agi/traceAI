/**
 * Jest setup, run before each test file loads any module. The send guard captures Node's own fetch once, when it loads, and always calls that
 * reference. Test files replace the global fetch with mocks and spies after the guard has loaded, so the reference the guard captures here is a
 * forwarder that calls whatever the test file has installed in the global at the time of the call (Node's own fetch when none is installed, or
 * when a spy calls through). The cells that must show the real behaviour, a replaced fetch that is never called, run in a child process that
 * does not load this file.
 */
const real = globalThis.fetch;
if (typeof real === "function") {
  let depth = 0;
  const forwarder = function fetch(...args) {
    if (depth > 0) return real.apply(globalThis, args);
    depth++;
    try {
      const live = globalThis.fetch;
      return (live === forwarder || typeof live !== "function" ? real : live).apply(globalThis, args);
    } finally { depth--; }
  };
  Object.defineProperty(globalThis, "fetch", { value: forwarder, writable: true, configurable: true, enumerable: true });
}
