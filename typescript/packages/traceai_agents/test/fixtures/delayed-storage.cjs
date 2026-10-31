// Test-only orphan scheduling delay; the real storage child owns the real lock.
require("../../dist/src/runtime/storage-child.js");
const [disconnect] = process.listeners("disconnect");
process.removeListener("disconnect", disconnect);
process.once("disconnect", () => setTimeout(disconnect, 1000));
