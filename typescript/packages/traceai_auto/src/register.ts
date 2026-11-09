// Keep bootstrap itself fail-open, including failures while loading dependencies.
// Do not use --import in NODE_OPTIONS: --require works in older Node too.
try {
  if (Number(process.versions.node.split(".")[0]) >= 18 && !require("node:worker_threads").isInternalThread) require("./index").init();
} catch {
  try { console.warn("[traceai:auto] Auto-instrumentation could not load; the application will continue."); } catch { /* Host logger. */ }
}
