#!/usr/bin/env node
"use strict";
// Passive post-action hooks only. Empty stdout is the documented neutral result.
// Module loading is process startup, measured separately from the 25ms input/IPC
// budget. Starting that timer before require could discard stdin without reading it.
try {
  const { observeCursor } = require("../dist/src/adapters/cursor/hook.js");
  const deadline = setTimeout(() => process.exit(0), 25);
  observeCursor(process.argv.slice(2))
    .catch(() => undefined)
    .finally(() => {
      clearTimeout(deadline);
      process.exit(0);
    });
} catch {
  process.exit(0);
}
