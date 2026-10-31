#!/usr/bin/env node
"use strict";
// Passive post-action hooks only. Empty stdout is the documented neutral result.
// Failures, including a missing build, never affect Cursor or print source data.
const deadline = setTimeout(() => process.exit(0), 25);
try {
  require("../dist/src/adapters/cursor/hook.js")
    .observeCursor(process.argv.slice(2))
    .catch(() => undefined)
    .finally(() => {
      clearTimeout(deadline);
      process.exit(0);
    });
} catch {
  clearTimeout(deadline);
  process.exit(0);
}
