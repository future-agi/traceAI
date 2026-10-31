// Test-only orphan scheduling delay; the real storage child owns the real lock.
const fs = require("node:fs");
const path = require("node:path");
let state;
process.on("message", (message) => {
  if (message.op === "init") state = path.dirname(message.args.root);
});
require("../../dist/src/runtime/storage-child.js");
const [disconnect] = process.listeners("disconnect");
process.removeListener("disconnect", disconnect);
process.once("disconnect", () => {
  fs.writeFileSync(path.join(state, "orphan-disconnected"), "yes", {
    mode: 0o600,
  });
  setTimeout(disconnect, 1000);
});
process.once("exit", () => {
  try {
    fs.writeFileSync(path.join(state, "orphan-exited"), "yes", { mode: 0o600 });
  } catch {
    // A failed test may already have removed its throwaway directory.
  }
});
